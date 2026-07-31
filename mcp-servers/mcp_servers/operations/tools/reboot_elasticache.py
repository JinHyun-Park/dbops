"""reboot_elasticache — approval-gated reboot of the primary cache cluster of a
replication group (reboot_cache_cluster). Brief disruption. Mirrors the write
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
from mcp_servers.shared.managed_tag_preflight import elasticache_cache_cluster_tag_warning

logger = logging.getLogger(__name__)


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
    except Exception:
        logger.warning(
            "elasticache describe_replication_groups failed for %s (rg=%s)",
            cluster_id, name, exc_info=True,
        )
        return {
            "status": "error",
            "reason": (
                "재부팅 대상 노드를 찾기 위한 replication group 조회에 실패했습니다 "
                "(자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }
    if not member:
        return {"status": "error", "reason": "재부팅할 노드를 찾지 못했습니다", "cluster_id": cluster_id}

    payload = {"target": cluster_id}
    if not approved:
        card = {"status": "approval_required", "cluster_id": cluster_id, "target": cluster_id,
                "member": member,
                "warnings": [f"재부팅은 해당 노드({member})를 잠시 중단시킵니다."]}
        # RebootCacheCluster is gated on the CACHE CLUSTER (the member node), not
        # on the replication group, so the group's tags would answer a different
        # question than IAM asks. Cross-account only, WARNING never a refusal.
        tag_warning = elasticache_cache_cluster_tag_warning(
            client, cluster_id, member, action="elasticache:RebootCacheCluster")
        if tag_warning:
            card["warning"] = tag_warning
        return card
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
    # One handler for ClientError and everything else. The approval is ALREADY
    # consumed here, so the short AWS error CODE stays in the response: it is a
    # bounded enum (InvalidCacheClusterState vs AccessDenied vs
    # CacheClusterNotFound) and without it the DBA has to burn a second approval
    # to learn which one it was. The exception MESSAGE, which carries the hub
    # account id, the platform role name and the target ARN, is logged only.
    except Exception as e:
        code = (
            e.response.get("Error", {}).get("Code", "")
            if isinstance(e, ClientError)
            else ""
        )
        code_part = f" ({code})" if code else ""
        logger.warning(
            "reboot_cache_cluster failed for %s (member=%s)",
            cluster_id, member, exc_info=True,
        )
        return {
            "status": "error",
            "reason": (
                f"재부팅 요청이 실패했습니다{code_part} (대상 노드={member}, 노드 목록 조회 또는 "
                "reboot_cache_cluster 호출 단계). 자세한 원인은 서버 로그를 확인하세요."
            ),
            "cluster_id": cluster_id,
        }
    return {"status": "ok", "cluster_id": cluster_id, "member": member}
