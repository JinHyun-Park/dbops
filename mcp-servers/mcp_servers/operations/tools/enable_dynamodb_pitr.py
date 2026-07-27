"""enable_dynamodb_pitr — approval-gated DynamoDB Point-in-Time Recovery toggle
(update_continuous_backups).

Turning PITR ON is a pure data-protection improvement. Turning it OFF is a
data-protection DEGRADATION, so disabling requires `force=true` (review fix #7) —
the force flag is ALSO hashed into the approval payload so the DBA approves the
forceful (disable) variant specifically, and Cedar `forbid`s the disable unless
force==true at the Gateway. Idempotent. TOCTOU-safe (execute-time re-read).
Cross-account via `client_for_cluster`. Never raises into the caller, and never
returns raw exception text: static Korean reason + module logger (an AWS error
message can carry the hub account id and the target table ARN, and this string is
rendered in chat).
"""

import logging

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, table_name_for_cluster

logger = logging.getLogger(__name__)


def _pitr_enabled(client, table: str) -> bool:
    """Current PITR status via describe_continuous_backups."""
    desc = client.describe_continuous_backups(TableName=table)
    cb = desc.get("ContinuousBackupsDescription") or {}
    pitr = cb.get("PointInTimeRecoveryDescription") or {}
    return pitr.get("PointInTimeRecoveryStatus") == "ENABLED"


def enable_dynamodb_pitr_impl(
    cache,
    cluster_id: str,
    enabled: bool = True,
    force: bool = False,
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    """update_continuous_backups to turn PITR on (enabled=True) or off
    (enabled=False, requires force=true). Approval-gated; never raises."""
    # Both flags must be real JSON booleans. A string flag is REFUSED, not
    # coerced: bare bool("false") is True, so an ambiguous value could make the
    # approved payload and the executed payload disagree while hashing the same
    # (and "false" would satisfy the force-to-disable rule below). request_approval
    # refuses it on the registration side for the same reason.
    for name, flag in (("enabled", enabled), ("force", force)):
        if not isinstance(flag, bool):
            return {
                "status": "error",
                "reason": (
                    f"{name}는 JSON boolean(true/false)이어야 합니다. 문자열 플래그는 "
                    "승인된 값과 실제 실행 값이 어긋날 수 있어 거부합니다."
                ),
                "cluster_id": cluster_id,
            }

    table = table_name_for_cluster(cluster_id)

    # Disabling PITR degrades data protection — refuse without an explicit force
    # (fix #7). Checked before the approval round-trip so we never burn an approval
    # on a shape the policy will block.
    if not enabled and not force:
        return {
            "status": "error",
            "reason": "PITR 비활성화는 데이터 보호 저하입니다 — force=true가 필요합니다.",
            "cluster_id": cluster_id,
        }

    try:
        client = client_for_cluster(cluster_id, "dynamodb")
        current = _pitr_enabled(client, table)
    except Exception:
        logger.warning("PITR describe failed for %s (table=%s)", cluster_id, table, exc_info=True)
        return {
            "status": "error",
            "reason": (
                "PITR 상태 조회에 실패했습니다. 적용 전 현재 상태를 확인할 수 없어 "
                "중단합니다 (자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }

    if current == enabled:
        return {
            "status": "skipped",
            "reason": "PITR이 이미 요청한 상태입니다 (변경 없음).",
            "cluster_id": cluster_id,
            "target": table,
            "enabled": enabled,
        }

    payload = {"enabled": enabled, "force": force}

    warnings = []
    if not enabled:
        warnings.append(
            "PITR을 끄면 35일 연속 백업 보호가 사라집니다. 복구 가능 시점이 즉시 단절됩니다."
        )

    if not approved:
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "target": table,
            "enabled": enabled,
            "force": force,
            "current_state": {"enabled": current},
            "warnings": warnings,
        }

    guard = verify_approval(
        approval_id, cluster_id, "enable_dynamodb_pitr", payload=payload
    )
    if not guard.get("ok"):
        return {
            "status": "approval_denied",
            "reason": guard.get("reason", "approval guard rejected the request"),
            "cluster_id": cluster_id,
        }

    # TOCTOU re-read (fix #6): confirm PITR hasn't already toggled since approval.
    try:
        fresh = _pitr_enabled(client, table)
    except Exception:
        logger.warning("PITR re-read failed for %s (table=%s)", cluster_id, table, exc_info=True)
        return {
            "status": "error",
            "reason": (
                "적용 직전 재조회에 실패해 안전을 위해 중단했습니다 "
                "(자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }
    if fresh != current:
        return {
            "status": "approval_denied",
            "reason": "table state changed since approval",
            "cluster_id": cluster_id,
        }

    try:
        client.update_continuous_backups(
            TableName=table,
            PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": enabled},
        )
    except Exception as e:
        # Only the short AWS error code (e.g. ValidationException) is echoed,
        # never the message, which carries the table ARN and the account id.
        code = (
            e.response.get("Error", {}).get("Code", "")
            if isinstance(e, ClientError)
            else ""
        )
        logger.warning(
            "update_continuous_backups failed for %s (table=%s)", cluster_id, table, exc_info=True
        )
        return {
            "status": "error",
            "reason": (
                f"update_continuous_backups 실패 ({code})" if code
                else "update_continuous_backups 실패 (자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }

    return {
        "status": "modified",
        "cluster_id": cluster_id,
        "target": table,
        "enabled": enabled,
    }
