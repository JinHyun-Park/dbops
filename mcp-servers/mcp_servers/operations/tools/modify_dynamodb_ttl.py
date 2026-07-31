"""modify_dynamodb_ttl — approval-gated DynamoDB TTL change (update_time_to_live).

Enables or disables an attribute TTL. Idempotent: if the table is already in the
requested state the tool reports `skipped` without burning the write (AWS also
rejects re-enabling an already-enabled TTL). Approval-gated and TOCTOU-safe — the
execute-time re-read confirms the table hasn't drifted from the approved state.
Cross-account via `client_for_cluster`. Never raises into the caller, and never
returns raw exception text: static Korean reason + module logger (an AWS error
message can carry the hub account id and the target table ARN, and this string is
rendered in chat).
"""

import logging

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, table_name_for_cluster
from mcp_servers.shared.managed_tag_preflight import dynamodb_table_tag_warning

logger = logging.getLogger(__name__)


def _ttl_state(client, table: str) -> dict:
    """Current TTL status + attribute via describe_time_to_live."""
    desc = client.describe_time_to_live(TableName=table)
    spec = desc.get("TimeToLiveDescription") or {}
    status = spec.get("TimeToLiveStatus") or "DISABLED"
    return {
        "enabled": status in ("ENABLED", "ENABLING"),
        "attribute": spec.get("AttributeName") or "",
    }


def modify_dynamodb_ttl_impl(
    cache,
    cluster_id: str,
    attribute: str = "",
    enabled: bool = True,
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    """update_time_to_live for `attribute`. enabled=True turns TTL on for the
    attribute; enabled=False turns it off. Approval-gated; never raises."""
    # `enabled` must be a real JSON boolean. A string flag is REFUSED, not
    # coerced: bare bool("false") is True, so an ambiguous value could make the
    # approved payload and the executed payload disagree while hashing the same.
    # request_approval refuses it on the registration side for the same reason.
    if not isinstance(enabled, bool):
        return {
            "status": "error",
            "reason": (
                "enabled는 JSON boolean(true/false)이어야 합니다. 문자열 플래그는 "
                "승인된 값과 실제 실행 값이 어긋날 수 있어 거부합니다."
            ),
            "cluster_id": cluster_id,
        }

    table = table_name_for_cluster(cluster_id)
    attribute = (attribute or "").strip()

    if not attribute:
        return {
            "status": "error",
            "reason": "TTL을 설정하려면 attribute(만료 타임스탬프 속성 이름)가 필요합니다.",
            "cluster_id": cluster_id,
        }

    try:
        client = client_for_cluster(cluster_id, "dynamodb")
        state = _ttl_state(client, table)
    except Exception:
        logger.warning("TTL describe failed for %s (table=%s)", cluster_id, table, exc_info=True)
        return {
            "status": "error",
            "reason": (
                "TTL 상태 조회에 실패했습니다. 적용 전 현재 상태를 확인할 수 없어 "
                "중단합니다 (자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }

    # Idempotent skip: already in the requested state (same attribute, same on/off).
    if state["enabled"] == enabled and (not enabled or state["attribute"] == attribute):
        return {
            "status": "skipped",
            "reason": "TTL이 이미 요청한 상태입니다 (변경 없음).",
            "cluster_id": cluster_id,
            "target": table,
            "attribute": attribute,
            "enabled": enabled,
        }

    payload = {"attribute": attribute, "enabled": enabled}

    warnings = ["TTL 변경은 테이블당 약 1시간에 한 번만 가능합니다 (AWS 제한)."]

    if not approved:
        card = {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "target": table,
            "attribute": attribute,
            "enabled": enabled,
            "current_state": state,
            "warnings": warnings,
        }
        # describe_time_to_live above does NOT carry the table ARN, so the helper
        # resolves it. Cross-account only, WARNING never a refusal.
        tag_warning = dynamodb_table_tag_warning(
            client, cluster_id, table, action="dynamodb:UpdateTimeToLive")
        if tag_warning:
            card["warning"] = tag_warning
        return card

    guard = verify_approval(
        approval_id, cluster_id, "modify_dynamodb_ttl", payload=payload
    )
    if not guard.get("ok"):
        return {
            "status": "approval_denied",
            "reason": guard.get("reason", "approval guard rejected the request"),
            "cluster_id": cluster_id,
        }

    # TOCTOU re-read (fix #6): confirm the table hasn't already reached/changed state.
    try:
        fresh = _ttl_state(client, table)
    except Exception:
        logger.warning("TTL re-read failed for %s (table=%s)", cluster_id, table, exc_info=True)
        return {
            "status": "error",
            "reason": (
                "적용 직전 재조회에 실패해 안전을 위해 중단했습니다 "
                "(자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }
    if fresh != state:
        return {
            "status": "approval_denied",
            "reason": "table state changed since approval",
            "cluster_id": cluster_id,
        }

    try:
        client.update_time_to_live(
            TableName=table,
            TimeToLiveSpecification={"Enabled": enabled, "AttributeName": attribute},
        )
    except Exception as e:
        # Only the short AWS error code (e.g. ValidationException) is echoed,
        # never the message, which carries the table ARN and the account id.
        code = (
            e.response.get("Error", {}).get("Code", "")
            if isinstance(e, ClientError)
            else ""
        )
        logger.warning("update_time_to_live failed for %s (table=%s)", cluster_id, table, exc_info=True)
        return {
            "status": "error",
            "reason": (
                f"update_time_to_live 실패 ({code})" if code
                else "update_time_to_live 실패 (자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }

    return {
        "status": "modified",
        "cluster_id": cluster_id,
        "target": table,
        "attribute": attribute,
        "enabled": enabled,
    }
