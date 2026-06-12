"""modify_dynamodb_ttl — approval-gated DynamoDB TTL change (update_time_to_live).

Enables or disables an attribute TTL. Idempotent: if the table is already in the
requested state the tool reports `skipped` without burning the write (AWS also
rejects re-enabling an already-enabled TTL). Approval-gated and TOCTOU-safe — the
execute-time re-read confirms the table hasn't drifted from the approved state.
Cross-account via `client_for_cluster`. Never raises into the caller.
"""

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster


def _table_name(cache, cluster_id: str) -> str:
    try:
        rows = cache.execute(
            "SELECT resource_name FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        ).rows
    except Exception:
        rows = []
    if rows and rows[0].get("resource_name"):
        return rows[0]["resource_name"]
    return cluster_id


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
    table = _table_name(cache, cluster_id)
    enabled = bool(enabled)
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
    except ClientError as e:
        return {
            "status": "error",
            "reason": f"TTL 상태 조회 실패 — 적용 전 현재 상태를 확인할 수 없어 중단합니다: {str(e)[:200]}",
            "cluster_id": cluster_id,
        }
    except Exception as e:
        return {
            "status": "error",
            "reason": f"TTL 상태 조회 실패: {str(e)[:200]}",
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
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "target": table,
            "attribute": attribute,
            "enabled": enabled,
            "current_state": state,
            "warnings": warnings,
        }

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
    except Exception as e:
        return {
            "status": "error",
            "reason": f"적용 직전 재조회 실패 — 안전을 위해 중단합니다: {str(e)[:200]}",
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
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        return {
            "status": "error",
            "reason": f"update_time_to_live 실패: {code or str(e)[:200]}",
            "cluster_id": cluster_id,
        }
    except Exception as e:
        return {
            "status": "error",
            "reason": f"update_time_to_live 실패: {str(e)[:200]}",
            "cluster_id": cluster_id,
        }

    return {
        "status": "modified",
        "cluster_id": cluster_id,
        "target": table,
        "attribute": attribute,
        "enabled": enabled,
    }
