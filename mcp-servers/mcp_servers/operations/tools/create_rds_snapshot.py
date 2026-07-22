"""create_rds_snapshot — approval-gated manual snapshot of a STANDALONE RDS DB
instance (non-Aurora: MySQL / SQL Server, the rds_instance engine family; R-3).

The handler positive-gates this tool on the rds_instance-only `instance_write`
capability (FAIL-CLOSED), so any other engine — or an unresolvable cluster —
gets unsupported_engine before the impl runs.

Non-destructive, but still approval-gated (it stands up a billable snapshot).
The default snapshot_id is resolved NOW in preview and hash-bound into the
approval — execute NEVER re-resolves it (mirrors add_reader_instance's billable-
value binding), so a stale/replayed approval can't create a differently-named
snapshot than the DBA approved.

FAIL-CLOSED: verify_approval must pass, a FRESH describe re-checks state right
before the call (TOCTOU), and no str(e) internals leak into a return.
"""

from datetime import datetime

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
        print(f"[create_rds_snapshot] describe_db_instances failed for {cluster_id}: {e}")
        return None
    return instances[0] if instances else None


def create_rds_snapshot_impl(
    cache: CacheClient,
    cluster_id: str,
    snapshot_id: str = "",
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    snapshot_id = (snapshot_id or "").strip()
    rds = client_for_cluster(cluster_id, "rds")

    inst = _describe(rds, cluster_id)
    if inst is None:
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": "인스턴스를 조회할 수 없습니다 — 대상 식별자를 확인하세요."}
    if inst.get("DBInstanceStatus") != "available":
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": f"인스턴스 상태가 available이 아닙니다 (현재: {inst.get('DBInstanceStatus')})."}

    if not approved:
        # Resolve the concrete snapshot id NOW so the approval hash binds the
        # exact identifier the DBA sees — execute never generates one after
        # approval.
        if not snapshot_id:
            snapshot_id = f"dbops-{cluster_id}-{datetime.utcnow().strftime('%Y%m%d%H%M')}"
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "snapshot_id": snapshot_id,
            "cli_preview": (
                f"RDS 인스턴스 스냅샷 생성: {cluster_id!r} 인스턴스의 수동 스냅샷 "
                f"{snapshot_id!r}을 생성합니다. 스냅샷은 스토리지 과금 대상이며 완료까지 수 분이 걸립니다."
            ),
        }

    guard = verify_approval(
        approval_id, cluster_id, "create_rds_snapshot",
        payload={"cluster_id": cluster_id, "snapshot_id": snapshot_id},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    # The id was resolved in PREVIEW and hash-bound by the approval — execute
    # uses the exact id the DBA approved and NEVER re-resolves an empty one.
    if not snapshot_id:
        return {"status": "snapshot_failed", "cluster_id": cluster_id,
                "reason": "snapshot_id가 승인에 바인딩되지 않았습니다 — 미리보기가 제안한 id로 다시 승인 요청하세요."}

    # TOCTOU: re-check on a FRESH describe immediately before the snapshot.
    fresh = _describe(rds, cluster_id)
    if fresh is None or fresh.get("DBInstanceStatus") != "available":
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": "승인 이후 인스턴스 상태가 바뀌었습니다 — 스냅샷을 생성하지 않았습니다."}

    try:
        rds.create_db_snapshot(
            DBInstanceIdentifier=cluster_id, DBSnapshotIdentifier=snapshot_id)
    except Exception as e:
        print(f"[create_rds_snapshot] create_db_snapshot failed for {cluster_id}: {e}")
        return {"status": "snapshot_failed", "cluster_id": cluster_id,
                "reason": "스냅샷 생성에 실패했습니다 (식별자 중복·상태·권한 확인)."}

    return {"status": "snapshot_creating", "cluster_id": cluster_id,
            "snapshot_id": snapshot_id, "db_status": "creating"}
