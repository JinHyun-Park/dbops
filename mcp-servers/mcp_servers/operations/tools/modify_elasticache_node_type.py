"""modify_elasticache_node_type — approval-gated ElastiCache node-type scaling
(modify_replication_group CacheNodeType). Mirrors the operations write model:
REQUEST describe → approval_required → verify_approval (consume) → EXECUTE.
Cross-account via client_for_cluster (control-plane API). Never raises out.

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

logger = logging.getLogger(__name__)


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
    # One handler for ClientError and everything else. The approval is ALREADY
    # consumed here, so the short AWS error CODE stays in the response: it is a
    # bounded enum (InvalidReplicationGroupState vs AccessDenied vs
    # InvalidParameterCombination) and without it the DBA has to burn a second
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
            "modify_replication_group failed for %s (rg=%s, node_type=%s)",
            cluster_id, name, node_type, exc_info=True,
        )
        return {
            "status": "error",
            "reason": (
                f"노드 타입 변경(modify_replication_group) 요청이 실패했습니다{code_part}. "
                f"현재={current}, 요청={node_type} (자세한 원인은 서버 로그를 확인하세요)."
            ),
            "cluster_id": cluster_id,
        }
    return {"status": "ok", "cluster_id": cluster_id, "node_type": node_type}
