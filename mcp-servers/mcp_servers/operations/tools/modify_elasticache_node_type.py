"""modify_elasticache_node_type — approval-gated ElastiCache node-type scaling
(modify_replication_group CacheNodeType). Mirrors the operations write model:
REQUEST describe → approval_required → verify_approval (consume) → EXECUTE.
Cross-account via client_for_cluster (control-plane API). Never raises out."""

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster


def _rg(client, name):
    rg = (client.describe_replication_groups(ReplicationGroupId=name).get("ReplicationGroups") or [])
    return rg[0] if rg else None


def modify_elasticache_node_type_impl(cache, cluster_id=None, node_type=None,
                                      approved=False, approval_id=None, **_):
    if not cluster_id or not node_type:
        return {"status": "invalid", "reason": "cluster_id와 node_type이 필요합니다", "cluster_id": cluster_id}
    row = lookup_cluster(cluster_id) or {}
    name = row.get("resource_name") or cluster_id
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        g = _rg(client, name)
    except Exception as e:
        return {"status": "error", "reason": f"조회 실패: {str(e)[:200]}", "cluster_id": cluster_id}
    if not g:
        return {"status": "error", "reason": "replication group을 찾지 못했습니다", "cluster_id": cluster_id}
    current = g.get("CacheNodeType", "")
    if current == node_type:
        return {"status": "invalid", "reason": f"이미 {node_type} 입니다", "cluster_id": cluster_id}

    payload = {"target": cluster_id, "node_type": node_type}
    if not approved:
        return {
            "status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
            "node_type": node_type, "current_state": {"node_type": current},
            "warnings": [f"노드 타입 변경은 적용 중 일시적 영향이 있을 수 있습니다. 현재={current} → 변경={node_type}"],
        }
    guard = verify_approval(approval_id, cluster_id, "modify_elasticache_node_type", payload=payload)
    if not guard.get("ok"):
        return {"status": "approval_denied", "reason": guard.get("reason", "approval guard rejected"), "cluster_id": cluster_id}
    try:
        client.modify_replication_group(ReplicationGroupId=name, CacheNodeType=node_type, ApplyImmediately=True)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        return {"status": "error", "reason": f"modify_replication_group 실패: {code or str(e)[:200]}", "cluster_id": cluster_id}
    except Exception as e:
        return {"status": "error", "reason": f"modify_replication_group 실패: {str(e)[:200]}", "cluster_id": cluster_id}
    return {"status": "ok", "cluster_id": cluster_id, "node_type": node_type}
