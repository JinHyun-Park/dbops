"""remove_reader_instance — approval-gated Aurora reader scale-IN (N-③).

Removes a READER instance from an Aurora cluster. The handler positive-gates
this tool on the relational-only `scale_instance` capability, so non-relational
engines get unsupported_engine before the impl runs.

SAFETY: the target must be a READER member of THIS cluster and must not be the
cluster's only instance — the writer is never deletable and a cluster is never
left with 0 instances.

FAIL-CLOSED like every write tool: verify_approval must pass before any RDS
write, and no str(e) internals leak into a return shown to users.
"""

import logging

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster
from mcp_servers.shared.managed_tag_preflight import (
    is_cross_account,
    rds_instance_tag_warning,
)


def remove_reader_instance_impl(
    cache: CacheClient,
    cluster_id: str,
    instance_id: str = "",
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    instance_id = (instance_id or "").strip()
    if not instance_id:
        return {"status": "invalid_instance", "cluster_id": cluster_id,
                "reason": "instance_id가 필요합니다."}

    if not approved:
        card = {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "instance_id": instance_id,
            "cli_preview": (
                f"리더 인스턴스 삭제 (scale-in): 클러스터 {cluster_id}에서 "
                f"{instance_id!r} 리더를 삭제합니다. 삭제는 비가역이며 writer·"
                "마지막 인스턴스는 보호됩니다. 삭제 전 이 리더로 가는 커스텀 "
                "엔드포인트/커넥션을 확인하세요."
            ),
        }
        # DeleteDBInstance is gated on the INSTANCE, not on the cluster, and the
        # rds client is only built on the execute path below.
        #
        # is_cross_account is checked HERE, before the client is resolved, not
        # left to the helper: this tool's preview is documented (and tested) to
        # return without resolving an rds client, and for a same-account cluster
        # there is no ResourceTag condition to warn about anyway. So the
        # invariant holds unchanged for same-account, and only a spoke-account
        # preview spends an assume-role plus two describes.
        #
        # WARNING never a refusal: see managed_tag_preflight. Wrapped because a
        # preflight nicety must never turn a working preview into an error.
        tag_warning = ""
        if is_cross_account(cluster_id):
            try:
                tag_warning = rds_instance_tag_warning(
                    client_for_cluster(cluster_id, "rds"), cluster_id, instance_id,
                    action="rds:DeleteDBInstance")
            except Exception:
                logging.getLogger(__name__).warning(
                    "ManagedBy preflight unavailable for %s (%s)",
                    cluster_id, instance_id, exc_info=True)
        if tag_warning:
            card["warning"] = tag_warning
        return card

    guard = verify_approval(
        approval_id, cluster_id, "remove_reader_instance",
        payload={"cluster_id": cluster_id, "instance_id": instance_id},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    rds = client_for_cluster(cluster_id, "rds")
    try:
        dbc = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except Exception as e:
        print(f"[remove_reader_instance] describe_db_clusters failed for {cluster_id}: {e}")
        return {"status": "remove_failed", "cluster_id": cluster_id,
                "reason": "클러스터 조회에 실패했습니다 — 대상 클러스터 식별자를 확인하세요."}

    members = dbc.get("DBClusterMembers") or []
    target = next((m for m in members if m.get("DBInstanceIdentifier") == instance_id), None)
    if target is None:
        return {"status": "instance_not_found", "cluster_id": cluster_id,
                "reason": f"{instance_id!r} 인스턴스가 이 클러스터의 멤버가 아닙니다."}
    if target.get("IsClusterWriter"):
        return {"status": "cannot_remove_writer", "cluster_id": cluster_id,
                "reason": "writer 인스턴스는 삭제할 수 없습니다."}
    if len(members) <= 1:
        return {"status": "cannot_remove_last_instance", "cluster_id": cluster_id,
                "reason": "마지막 인스턴스는 삭제할 수 없습니다."}

    # Re-verify on a FRESH member list immediately before delete: a failover in
    # the window since the early guards could have promoted this reader to
    # writer. This shrinks the writer-protection TOCTOU to the API-call minimum.
    try:
        fresh = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    except Exception as e:
        print(f"[remove_reader_instance] pre-delete re-describe failed for {cluster_id}: {e}")
        return {"status": "remove_failed", "cluster_id": cluster_id,
                "reason": "삭제 직전 클러스터 재확인에 실패했습니다 — 안전을 위해 삭제하지 않았습니다."}
    fresh_members = fresh.get("DBClusterMembers") or []
    fresh_target = next(
        (m for m in fresh_members if m.get("DBInstanceIdentifier") == instance_id), None)
    if fresh_target is None:
        return {"status": "instance_not_found", "cluster_id": cluster_id,
                "reason": f"{instance_id!r} 인스턴스가 이 클러스터의 멤버가 아닙니다."}
    if fresh_target.get("IsClusterWriter"):
        return {"status": "cannot_remove_writer", "cluster_id": cluster_id,
                "reason": "writer 인스턴스는 삭제할 수 없습니다."}
    if len(fresh_members) <= 1:
        return {"status": "cannot_remove_last_instance", "cluster_id": cluster_id,
                "reason": "마지막 인스턴스는 삭제할 수 없습니다."}

    try:
        # Aurora cluster instances take no final snapshot — do NOT pass
        # SkipFinalSnapshot (the API rejects it for cluster members).
        rds.delete_db_instance(DBInstanceIdentifier=instance_id)
    except Exception as e:
        print(f"[remove_reader_instance] delete_db_instance failed for {instance_id}: {e}")
        return {"status": "remove_failed", "cluster_id": cluster_id,
                "reason": "인스턴스 삭제에 실패했습니다 (상태·권한 확인)."}

    return {
        "status": "instance_removing",
        "cluster_id": cluster_id,
        "instance_id": instance_id,
        "db_status": "deleting",
    }
