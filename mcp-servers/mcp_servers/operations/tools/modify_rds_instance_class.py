"""modify_rds_instance_class — approval-gated compute resize of a STANDALONE RDS
DB instance (non-Aurora: MySQL / SQL Server, the rds_instance engine family; R-3).

The handler positive-gates this tool on the rds_instance-only `instance_write`
capability (FAIL-CLOSED), so any other engine — or an unresolvable cluster —
gets unsupported_engine before the impl runs.

The instance's CURRENT class is read in preview and hash-bound into the approval
alongside the target. Execute re-reads it on a FRESH describe (TOCTOU): if the
class drifted since approval (someone else resized), the change is refused rather
than applied on top of an unexpected state. modify_db_instance runs with
ApplyImmediately=True.

FAIL-CLOSED: verify_approval must pass; no str(e) internals leak into a return.
"""

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster


def _describe(rds, cluster_id):
    """Return the instance dict or None. Never raises."""
    try:
        instances = rds.describe_db_instances(DBInstanceIdentifier=cluster_id).get(
            "DBInstances"
        ) or []
    except Exception as e:
        print(f"[modify_rds_instance_class] describe_db_instances failed for {cluster_id}: {e}")
        return None
    return instances[0] if instances else None


def modify_rds_instance_class_impl(
    cache: CacheClient,
    cluster_id: str,
    target_class: str = "",
    current_class: str = "",
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    target_class = (target_class or "").strip()
    current_class = (current_class or "").strip()

    if not target_class:
        return {"status": "invalid_request", "cluster_id": cluster_id,
                "reason": "target_class가 필요합니다 (예: db.r6g.large)."}

    rds = client_for_cluster(cluster_id, "rds")

    if not approved:
        inst = _describe(rds, cluster_id)
        if inst is None:
            return {"status": "not_applicable", "cluster_id": cluster_id,
                    "reason": "인스턴스를 조회할 수 없습니다 — 대상 식별자를 확인하세요."}
        if inst.get("DBInstanceStatus") != "available":
            return {"status": "not_applicable", "cluster_id": cluster_id,
                    "reason": f"인스턴스 상태가 available이 아닙니다 (현재: {inst.get('DBInstanceStatus')})."}
        live_class = inst.get("DBInstanceClass") or ""
        if live_class == target_class:
            return {"status": "not_applicable", "cluster_id": cluster_id,
                    "reason": f"인스턴스가 이미 {target_class!r} 클래스입니다 (변경 없음)."}
        # Bind the current class NOW so the approval hash pins the baseline
        # state; execute refuses if the live class has drifted from it.
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "target_class": target_class,
            "current_class": live_class,
            "cli_preview": (
                f"RDS 인스턴스 클래스 변경: {cluster_id!r} 인스턴스를 "
                f"{live_class!r} → {target_class!r}로 변경합니다 (ApplyImmediately). "
                "인스턴스 재기동으로 짧은 다운타임이 발생하며 시간당 비용이 달라집니다."
            ),
        }

    guard = verify_approval(
        approval_id, cluster_id, "modify_rds_instance_class",
        payload={"cluster_id": cluster_id, "target_class": target_class,
                 "current_class": current_class},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    # TOCTOU: re-read the live class and refuse if it drifted from the class the
    # approval was bound to — never resize on top of an unexpected state.
    fresh = _describe(rds, cluster_id)
    if fresh is None or fresh.get("DBInstanceStatus") != "available":
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": "승인 이후 인스턴스 상태가 바뀌었습니다 — 변경하지 않았습니다."}
    if (fresh.get("DBInstanceClass") or "") != current_class:
        return {"status": "state_changed", "cluster_id": cluster_id,
                "reason": "승인 이후 인스턴스 클래스가 변경되었습니다 — 안전을 위해 변경하지 않았습니다. 다시 승인 요청하세요."}

    try:
        rds.modify_db_instance(
            DBInstanceIdentifier=cluster_id, DBInstanceClass=target_class,
            ApplyImmediately=True)
    except Exception as e:
        print(f"[modify_rds_instance_class] modify_db_instance failed for {cluster_id}: {e}")
        return {"status": "modify_failed", "cluster_id": cluster_id,
                "reason": "인스턴스 클래스 변경에 실패했습니다 (클래스 유효성·상태·권한 확인)."}

    return {"status": "modifying", "cluster_id": cluster_id,
            "target_class": target_class, "db_status": "modifying"}
