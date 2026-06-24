"""create_elasticache_snapshot — approval-gated ElastiCache (Redis/Valkey) backup
(create_snapshot). Memcached has no snapshots → unsupported_engine. Mirrors the
operations write model. Never raises out."""

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster


def create_elasticache_snapshot_impl(cache, cluster_id=None, snapshot_name=None,
                                     approved=False, approval_id=None, **_):
    if not cluster_id or not snapshot_name:
        return {"status": "invalid", "reason": "cluster_id와 snapshot_name이 필요합니다", "cluster_id": cluster_id}
    row = lookup_cluster(cluster_id) or {}
    rd = row.get("resource_details") or {}
    engine = (rd.get("engine") or row.get("engine") or "redis").lower()
    if engine == "memcached":
        return {"status": "unsupported_engine", "reason": "Memcached는 스냅샷을 지원하지 않습니다", "cluster_id": cluster_id}
    name = row.get("resource_name") or cluster_id

    payload = {"target": cluster_id, "snapshot_name": snapshot_name}
    if not approved:
        return {
            "status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
            "snapshot_name": snapshot_name,
            "warnings": ["스냅샷 생성은 메모리/성능에 일시적 영향이 있을 수 있습니다(특히 단일 노드)."],
        }
    guard = verify_approval(approval_id, cluster_id, "create_elasticache_snapshot", payload=payload)
    if not guard.get("ok"):
        return {"status": "approval_denied", "reason": guard.get("reason", "approval guard rejected"), "cluster_id": cluster_id}
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        client.create_snapshot(ReplicationGroupId=name, SnapshotName=snapshot_name)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        return {"status": "error", "reason": f"create_snapshot 실패: {code or str(e)[:200]}", "cluster_id": cluster_id}
    return {"status": "ok", "cluster_id": cluster_id, "snapshot_name": snapshot_name}
