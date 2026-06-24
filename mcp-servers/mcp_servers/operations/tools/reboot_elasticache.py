"""reboot_elasticache — approval-gated reboot of the primary cache cluster of a
replication group (reboot_cache_cluster). Brief disruption. Mirrors the write
model; never raises out."""

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster


def _primary_member(client, name):
    rg = (client.describe_replication_groups(ReplicationGroupId=name).get("ReplicationGroups") or [])
    if not rg:
        return None
    members = rg[0].get("MemberClusters") or []
    return members[0] if members else None


def reboot_elasticache_impl(cache, cluster_id=None, approved=False, approval_id=None, **_):
    if not cluster_id:
        return {"status": "invalid", "reason": "cluster_id가 필요합니다"}
    row = lookup_cluster(cluster_id) or {}
    name = row.get("resource_name") or cluster_id
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        member = _primary_member(client, name)
    except Exception as e:
        return {"status": "error", "reason": f"조회 실패: {str(e)[:200]}", "cluster_id": cluster_id}
    if not member:
        return {"status": "error", "reason": "재부팅할 노드를 찾지 못했습니다", "cluster_id": cluster_id}

    payload = {"target": cluster_id}
    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
                "member": member, "warnings": [f"재부팅은 해당 노드({member})를 잠시 중단시킵니다."]}
    guard = verify_approval(approval_id, cluster_id, "reboot_elasticache", payload=payload)
    if not guard.get("ok"):
        return {"status": "approval_denied", "reason": guard.get("reason", "approval guard rejected"), "cluster_id": cluster_id}
    try:
        # resolve node ids for the member cache cluster
        cc = (client.describe_cache_clusters(CacheClusterId=member, ShowCacheNodeInfo=True).get("CacheClusters") or [])
        node_ids = [n.get("CacheNodeId") for n in (cc[0].get("CacheNodes") or [])] if cc else []
        if not node_ids:
            node_ids = ["0001"]
        client.reboot_cache_cluster(CacheClusterId=member, CacheNodeIdsToReboot=node_ids)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        return {"status": "error", "reason": f"reboot_cache_cluster 실패: {code or str(e)[:200]}", "cluster_id": cluster_id}
    return {"status": "ok", "cluster_id": cluster_id, "member": member}
