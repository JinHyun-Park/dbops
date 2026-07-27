"""simulate_elasticache_node_resize — estimate the monthly cost of resizing an
ElastiCache cluster's node type / count. Read-only (describe + Price List API),
no approval. Cross-account via client_for_cluster.

Failures return a STATIC Korean reason and log the detail with the module
logger: raw exception text must never reach a tool response (an ElastiCache
describe error carries the hub account id, the platform role name and the
replication-group ARN)."""

import logging

from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster
from mcp_servers.shared.elasticache_cost import compute_node_resize_cost

logger = logging.getLogger(__name__)


def _current(client, name):
    rg = (client.describe_replication_groups(ReplicationGroupId=name).get("ReplicationGroups") or [])
    if not rg:
        return None, None
    g = rg[0]
    node_type = g.get("CacheNodeType", "")
    count = len(g.get("MemberClusters") or []) or 1
    return node_type, count


def simulate_elasticache_node_resize_impl(cache, cluster_id=None, new_node_type=None,
                                          new_node_count=None, **_):
    if not cluster_id:
        return {"status": "error", "reason": "cluster_id가 필요합니다"}
    row = lookup_cluster(cluster_id) or {}
    rd = row.get("resource_details") or {}
    engine = (rd.get("engine") or row.get("engine") or "redis").lower()
    region = row.get("region", "")
    name = row.get("resource_name") or cluster_id
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        cur_type, cur_count = _current(client, name)
    except Exception:
        logger.warning(
            "elasticache describe failed for %s (group=%s, region=%s)",
            cluster_id, name, region, exc_info=True,
        )
        return {
            "status": "error",
            "reason": (
                "ElastiCache 클러스터의 현재 구성을 조회할 수 없습니다 "
                "(자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }
    if not cur_type:
        return {"status": "error", "reason": "replication group을 찾지 못했습니다", "cluster_id": cluster_id}
    result = compute_node_resize_cost(
        engine=engine, region=region, current_node_type=cur_type,
        current_node_count=cur_count, new_node_type=new_node_type,
        new_node_count=new_node_count)
    result["cluster_id"] = cluster_id
    return result
