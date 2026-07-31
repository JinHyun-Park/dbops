"""test_elasticache_failover — approval-gated failover test (test_failover) for a
replication group that HAS a replica. No replica → invalid. Mirrors the write
model; never raises out.

Failures return a STATIC Korean reason and log the detail with the module
logger: the raw exception MESSAGE must never reach a tool response (an AWS error
carries the hub account id, the platform role name and the target ARN, and the
pre-approval describe below is reachable by any chat user). The post-approval
write path additionally reports the bounded AWS error CODE, because by then the
single-use approval is spent."""

import logging

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster
from mcp_servers.shared.managed_tag_preflight import elasticache_group_tag_warning

logger = logging.getLogger(__name__)


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
    except Exception:
        logger.warning(
            "elasticache describe_replication_groups failed for %s (rg=%s)",
            cluster_id, name, exc_info=True,
        )
        return {
            "status": "error",
            "reason": "replication group 조회에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요).",
            "cluster_id": cluster_id,
        }
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
        card = {"status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
                "node_group_id": ngid,
                "warnings": [f"failover 테스트는 노드그룹 {ngid}의 프라이머리를 전환합니다."]}
        # Cross-account only, WARNING never a refusal: see managed_tag_preflight.
        tag_warning = elasticache_group_tag_warning(
            client, cluster_id, name, action="elasticache:TestFailover")
        if tag_warning:
            card["warning"] = tag_warning
        return card
    guard = verify_approval(approval_id, cluster_id, "test_elasticache_failover", payload=payload)
    if not guard.get("ok"):
        return {"status": "approval_denied", "reason": guard.get("reason", "approval guard rejected"), "cluster_id": cluster_id}
    try:
        client.test_failover(ReplicationGroupId=name, NodeGroupId=ngid)
    # One handler for ClientError and everything else. The approval is ALREADY
    # consumed here, so the short AWS error CODE stays in the response: it is a
    # bounded enum (TestFailoverNotAvailableFault vs AccessDenied vs
    # InvalidReplicationGroupState) and without it the DBA has to burn a second
    # approval to learn which one it was. The exception MESSAGE, which carries the
    # hub account id, the platform role name and the target ARN, is logged only.
    except Exception as e:
        code = (
            e.response.get("Error", {}).get("Code", "")
            if isinstance(e, ClientError)
            else ""
        )
        code_part = f" ({code})" if code else ""
        logger.warning(
            "test_failover failed for %s (rg=%s, node_group=%s)",
            cluster_id, name, ngid, exc_info=True,
        )
        return {
            "status": "error",
            "reason": (
                f"failover 테스트(test_failover) 요청이 실패했습니다{code_part} (노드그룹={ngid}). "
                "자세한 원인은 서버 로그를 확인하세요. 최근에 failover를 실행했거나 "
                "클러스터가 available 상태가 아닐 수 있습니다."
            ),
            "cluster_id": cluster_id,
        }
    return {"status": "ok", "cluster_id": cluster_id, "node_group_id": ngid}
