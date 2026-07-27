"""modify_dynamodb_capacity — approval-gated DynamoDB capacity / billing-mode
change (update_table), mirroring the operations-server write safety model.

Two reads bracket the (approval-gated) write:
  - REQUEST-time describe_table surfaces AWS constraints (GSIs, current mode) as
    `approval_required` warnings, and BLOCKS outright on any GSI (review fix #5 —
    GSIs don't inherit table throughput; per-GSI capacity is a v2 follow-up).
  - EXECUTE-time re-read IMMEDIATELY before update_table defeats TOCTOU (fix #6):
    the approval binds an expected current-state precondition; if the table drifted
    (mode already switched, a GSI appeared) we ABORT with approval_denied rather
    than apply a now-different effective change.

Effective values are validated (RCU/WCU >= 1, fix #4) BEFORE hashing/verification so
the hashed value == the executed value — never floored after the hash. All AWS calls
go through `client_for_cluster` (hub-spoke cross-account aware). Never raises into the
caller: any boto3/guard error degrades to `{"status":"error", reason}` with a STATIC
Korean reason, and the detail goes to the module logger. The raw exception MESSAGE must
never reach a tool response (an AWS error carries the hub account id, the platform role
name and the target table ARN, and the request-time describe below is reachable by any
chat user before an approval exists). The post-approval update_table path additionally
reports the bounded AWS error CODE, because by then the single-use approval is spent.
"""

import logging

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, table_name_for_cluster

logger = logging.getLogger(__name__)

_VALID_MODES = ("PROVISIONED", "PAY_PER_REQUEST")


def _norm_mode(billing_mode):
    """Normalise a caller-supplied billing mode to an AWS BillingMode token, or
    "" when not requesting a mode switch. Accepts the friendly aliases the agent
    / cost simulator emit (On-Demand, Provisioned)."""
    if not billing_mode:
        return ""
    m = str(billing_mode).strip().upper().replace("-", "_").replace(" ", "_")
    if m in ("ON_DEMAND", "ONDEMAND", "PAY_PER_REQUEST", "PAYPERREQUEST"):
        return "PAY_PER_REQUEST"
    if m in ("PROVISIONED",):
        return "PROVISIONED"
    return m  # surfaced as invalid below


def _current_state(client, table: str) -> dict:
    """describe_table → the fields that define our precondition + warnings:
    billing mode, provisioned rcu/wcu, and whether any GSI exists."""
    desc = client.describe_table(TableName=table)["Table"]
    summary = desc.get("BillingModeSummary") or {}
    mode = summary.get("BillingMode") or (
        "PROVISIONED" if (desc.get("ProvisionedThroughput") or {}).get("ReadCapacityUnits") else ""
    )
    pt = desc.get("ProvisionedThroughput") or {}
    gsis = desc.get("GlobalSecondaryIndexes") or []
    return {
        "billing_mode": mode or "",
        "rcu": int(pt.get("ReadCapacityUnits") or 0),
        "wcu": int(pt.get("WriteCapacityUnits") or 0),
        "gsi_names": sorted(g.get("IndexName", "") for g in gsis),
    }


def _validate_capacity(target_mode: str, rcu, wcu):
    """Validate the requested capacity for the EFFECTIVE target mode.

    Returns (effective_rcu, effective_wcu, error) where error is None on success.
    Provisioned requires both RCU/WCU; each must be an integer >= 1 (fix #4 — reject
    <1 rather than silently flooring, so the hashed value equals the executed value).
    On-Demand drops capacity (returns None/None)."""
    if target_mode == "PAY_PER_REQUEST":
        return None, None, None
    # PROVISIONED (either explicit switch or an in-place capacity change).
    if rcu is None or wcu is None:
        return None, None, "Provisioned 모드는 RCU와 WCU를 모두 지정해야 합니다."
    try:
        rcu_i = int(rcu)
        wcu_i = int(wcu)
    except (TypeError, ValueError):
        return None, None, "RCU/WCU는 정수여야 합니다."
    if rcu_i != float(rcu) or wcu_i != float(wcu):
        return None, None, "RCU/WCU는 정수여야 합니다 (소수점 불가)."
    if rcu_i < 1 or wcu_i < 1:
        return None, None, "RCU/WCU는 최소 1 이상이어야 합니다 (1 미만은 거부)."
    return rcu_i, wcu_i, None


def modify_dynamodb_capacity_impl(
    cache,
    cluster_id: str,
    billing_mode: str = "",
    rcu: int = None,
    wcu: int = None,
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    """update_table to switch billing mode (Provisioned<->On-Demand) and/or set
    provisioned RCU/WCU. Approval-gated; never raises."""
    table = table_name_for_cluster(cluster_id)

    # --- request-time describe: surface constraints + BLOCK on GSI (fix #5) ---
    try:
        client = client_for_cluster(cluster_id, "dynamodb")
        state = _current_state(client, table)
    # One handler for ClientError and everything else: both returned the same
    # status, and the only difference in the message was the exception text.
    except Exception:
        logger.warning("describe_table failed for %s (table=%s)", cluster_id, table, exc_info=True)
        return {
            "status": "error",
            "reason": (
                f"테이블 조회에 실패했습니다 (대상 테이블={table}). 적용 전 현재 상태를 "
                "확인할 수 없어 중단합니다 (자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }

    if state["gsi_names"]:
        return {
            "status": "unsupported",
            "reason": (
                "per-GSI capacity not supported in v1; change via Console/CDK "
                f"(GSIs: {', '.join(state['gsi_names'])})"
            ),
            "cluster_id": cluster_id,
        }

    requested_mode = _norm_mode(billing_mode)
    if requested_mode and requested_mode not in _VALID_MODES:
        return {
            "status": "error",
            "reason": f"billing_mode 값이 올바르지 않습니다: {billing_mode!r} (PROVISIONED 또는 On-Demand)",
            "cluster_id": cluster_id,
        }

    # The EFFECTIVE target mode: an explicit switch, else the table's current mode.
    target_mode = requested_mode or state["billing_mode"] or "PROVISIONED"

    eff_rcu, eff_wcu, verr = _validate_capacity(target_mode, rcu, wcu)
    if verr:
        return {"status": "error", "reason": verr, "cluster_id": cluster_id}

    # The payload the approval is bound to — built from EFFECTIVE (validated >=1)
    # values + the explicit table target so no user-controllable field is outside
    # the hash (fix #1). force is unused for capacity in v1 but bound for parity.
    payload = {
        "target": table,
        "billing_mode": requested_mode,
        "rcu": eff_rcu,
        "wcu": eff_wcu,
        "force": False,
    }

    warnings = []
    if requested_mode and requested_mode != state["billing_mode"]:
        warnings.append(
            "빌링 모드 전환은 AWS가 rate-limit 합니다 (테이블당 제한). "
            f"현재={state['billing_mode'] or 'unknown'} → 변경={requested_mode}"
        )

    if not approved:
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "target": table,
            "billing_mode": requested_mode,
            "rcu": eff_rcu,
            "wcu": eff_wcu,
            "current_state": {
                "billing_mode": state["billing_mode"],
                "rcu": state["rcu"],
                "wcu": state["wcu"],
            },
            "warnings": warnings,
        }

    guard = verify_approval(
        approval_id, cluster_id, "modify_dynamodb_capacity", payload=payload
    )
    if not guard.get("ok"):
        return {
            "status": "approval_denied",
            "reason": guard.get("reason", "approval guard rejected the request"),
            "cluster_id": cluster_id,
        }

    # --- TOCTOU re-read (fix #6): re-describe IMMEDIATELY before the write ---
    try:
        fresh = _current_state(client, table)
    except Exception:
        logger.warning(
            "pre-write describe_table failed for %s (table=%s)", cluster_id, table, exc_info=True
        )
        return {
            "status": "error",
            "reason": (
                "적용 직전 재조회에 실패했습니다. 안전을 위해 변경하지 않고 중단합니다 "
                "(자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }
    if fresh["gsi_names"]:
        # A GSI appeared between request and execute — the approved change no
        # longer applies cleanly. Refuse rather than mis-apply.
        return {
            "status": "approval_denied",
            "reason": "table state changed since approval",
            "cluster_id": cluster_id,
        }
    if fresh["billing_mode"] != state["billing_mode"]:
        return {
            "status": "approval_denied",
            "reason": "table state changed since approval",
            "cluster_id": cluster_id,
        }
    # In-place capacity change (no mode switch): if the table's current capacity
    # already drifted from what we observed at request time, the approved effective
    # change is now different — abort.
    if (
        not requested_mode
        and target_mode == "PROVISIONED"
        and (fresh["rcu"], fresh["wcu"]) != (state["rcu"], state["wcu"])
    ):
        return {
            "status": "approval_denied",
            "reason": "table state changed since approval",
            "cluster_id": cluster_id,
        }

    params = {"TableName": table}
    if requested_mode:
        params["BillingMode"] = requested_mode
    if target_mode == "PROVISIONED":
        params["ProvisionedThroughput"] = {
            "ReadCapacityUnits": eff_rcu,
            "WriteCapacityUnits": eff_wcu,
        }

    try:
        client.update_table(**params)
    # One handler for ClientError and everything else. The approval is ALREADY
    # consumed here, so the short AWS error CODE stays in the response: it is a
    # bounded enum (LimitExceededException vs AccessDenied vs
    # InvalidParameterCombination) and without it the DBA has to burn a second
    # approval to learn which one it was. The exception MESSAGE, which carries the
    # hub account id and the table ARN, goes to CloudWatch only.
    except Exception as e:
        code = (
            e.response.get("Error", {}).get("Code", "")
            if isinstance(e, ClientError)
            else ""
        )
        code_part = f" ({code})" if code else ""
        logger.warning(
            "update_table failed for %s (table=%s, mode=%s, rcu=%s, wcu=%s)",
            cluster_id, table, target_mode, eff_rcu, eff_wcu, exc_info=True,
        )
        return {
            "status": "error",
            "reason": (
                f"용량 변경(update_table) 요청이 실패했습니다{code_part} (대상 테이블={table}, "
                f"모드={target_mode}, RCU={eff_rcu}, WCU={eff_wcu}). 자세한 원인은 서버 "
                "로그를 확인하세요. 빌링 모드 전환 rate-limit 또는 계정 용량 한도일 수 "
                "있습니다."
            ),
            "cluster_id": cluster_id,
        }

    return {
        "status": "modified",
        "cluster_id": cluster_id,
        "target": table,
        "billing_mode": target_mode,
        "rcu": eff_rcu,
        "wcu": eff_wcu,
    }
