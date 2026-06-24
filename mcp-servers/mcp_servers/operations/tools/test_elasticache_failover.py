"""test_elasticache_failover — approval-gated failover test (test_failover) for a
replication group that HAS a replica. No replica → invalid. Mirrors the write
model; never raises out."""

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster


def test_elasticache_failover_impl(cache, cluster_id=None, node_group_id=None,
                                   approved=False, approval_id=None, **_):
    if not cluster_id:
        return {"status": "invalid", "reason": "cluster_id가 필요합니다"}
    row = lookup_cluster(cluster_id) or {}
    rd = row.get("resource_details") or {}
    if (rd.get("engine") or "redis").lower() == "memcached":
        return {"status": "unsupported_engine", "reason": "Memcached는 failover를 지원하지 않습니다", "cluster_id": cluster_id}
    name = row.get("resource_name") or cluster_id
    try:
        client = client_for_cluster(cluster_id, "elasticache")
        rg = (client.describe_replication_groups(ReplicationGroupId=name).get("ReplicationGroups") or [])
    except Exception as e:
        return {"status": "error", "reason": f"조회 실패: {str(e)[:200]}", "cluster_id": cluster_id}
    if not rg:
        return {"status": "error", "reason": "replication group을 찾지 못했습니다", "cluster_id": cluster_id}
    node_groups = rg[0].get("NodeGroups") or []
    # default to the first node group; require a replica (>=2 members) to fail over
    ng = None
    for g in node_groups:
        if node_group_id is None or g.get("NodeGroupId") == node_group_id:
            ng = g
            break
    if not ng:
        return {"status": "invalid", "reason": "node group을 찾지 못했습니다", "cluster_id": cluster_id}
    if len(ng.get("NodeGroupMembers") or []) < 2:
        return {"status": "invalid", "reason": "failover하려면 레플리카가 1개 이상 필요합니다", "cluster_id": cluster_id}
    ngid = ng.get("NodeGroupId")

    payload = {"target": cluster_id, "node_group_id": ngid}
    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
                "node_group_id": ngid, "warnings": [f"failover 테스트는 노드그룹 {ngid}의 프라이머리를 전환합니다."]}
    guard = verify_approval(approval_id, cluster_id, "test_elasticache_failover", payload=payload)
    if not guard.get("ok"):
        return {"status": "approval_denied", "reason": guard.get("reason", "approval guard rejected"), "cluster_id": cluster_id}
    try:
        client.test_failover(ReplicationGroupId=name, NodeGroupId=ngid)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        return {"status": "error", "reason": f"test_failover 실패: {code or str(e)[:200]}", "cluster_id": cluster_id}
    except Exception as e:
        return {"status": "error", "reason": f"test_failover 실패: {str(e)[:200]}", "cluster_id": cluster_id}
    return {"status": "ok", "cluster_id": cluster_id, "node_group_id": ngid}
