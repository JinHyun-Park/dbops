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

import logging
from datetime import datetime

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster

logger = logging.getLogger(__name__)

# COULD-NOT-ASK, as distinct from DOES-NOT-EXIST. See _describe.
_LOOKUP_FAILED = object()


def _describe(rds, cluster_id):
    """Return the instance dict, None when RDS says there is no such instance, or
    _LOOKUP_FAILED when the describe could not be made at all. Never raises.

    Those last two are DIFFERENT answers, and collapsing them into one None sent
    the DBA to check the target identifier after a throttle or an AccessDenied,
    where the identifier is the one thing that is not the problem. Same split
    modify_rds_instance_params already ships.
    """
    try:
        instances = rds.describe_db_instances(DBInstanceIdentifier=cluster_id).get(
            "DBInstances"
        ) or []
    except Exception:
        logger.warning("describe_db_instances failed for %s", cluster_id, exc_info=True)
        return _LOOKUP_FAILED
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
    if inst is _LOOKUP_FAILED:
        return {"status": "lookup_failed", "cluster_id": cluster_id,
                "reason": ("RDS describe 호출 자체가 실패해 인스턴스 상태를 확인하지 "
                           "못했습니다 (throttling 또는 권한 문제일 수 있습니다). "
                           "대상 식별자 문제가 아니므로 잠시 후 다시 시도하고, 반복되면 "
                           "IAM 권한을 확인하세요. 승인은 아직 요청되지 않았습니다.")}
    if inst is None:
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": "해당 식별자의 RDS 인스턴스가 존재하지 않습니다. 대상 식별자를 확인하세요."}
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
    if fresh is _LOOKUP_FAILED:
        # The approval is SINGLE-USE and was consumed above, so say so: this is the
        # one place the DBA has to re-request, and a message that only says
        # "state changed" points them at a state that may not have changed at all.
        return {"status": "lookup_failed", "cluster_id": cluster_id,
                "reason": ("스냅샷 직전 재확인을 위한 RDS describe 호출이 실패해 안전을 위해 "
                           "중단했습니다 (throttling 또는 권한 문제일 수 있습니다). "
                           "이 승인은 이미 소진되었으므로 스냅샷이 필요하면 승인을 다시 "
                           "요청해야 합니다.")}
    if fresh is None:
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": ("승인 이후 해당 인스턴스가 더 이상 존재하지 않습니다. 스냅샷을 "
                           "생성하지 않았으며, 이 승인은 이미 소진되었습니다.")}
    if fresh.get("DBInstanceStatus") != "available":
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": (f"승인 이후 인스턴스 상태가 바뀌었습니다 (현재: "
                           f"{fresh.get('DBInstanceStatus')}). 스냅샷을 생성하지 않았습니다.")}

    try:
        rds.create_db_snapshot(
            DBInstanceIdentifier=cluster_id, DBSnapshotIdentifier=snapshot_id)
    except Exception:
        logger.warning("create_db_snapshot failed for %s", cluster_id, exc_info=True)
        return {"status": "snapshot_failed", "cluster_id": cluster_id,
                "reason": "스냅샷 생성에 실패했습니다 (식별자 중복·상태·권한 확인)."}

    return {"status": "snapshot_creating", "cluster_id": cluster_id,
            "snapshot_id": snapshot_id, "db_status": "creating"}
