"""create_elasticache_snapshot — approval-gated ElastiCache (Redis/Valkey) backup
(create_snapshot). Memcached has no snapshots → unsupported_engine. Mirrors the
operations write model. Never raises out.

Failures return a STATIC Korean reason and log the detail with the module
logger: the raw exception MESSAGE must never reach a tool response (an AWS error
carries the hub account id, the platform role name and the target ARN). The
post-approval write path additionally reports the bounded AWS error CODE, because
by then the single-use approval is spent."""

import logging

from botocore.exceptions import ClientError

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cluster_targets import client_for_cluster, lookup_cluster

logger = logging.getLogger(__name__)


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
    # One handler for ClientError and everything else. The approval is ALREADY
    # consumed here, so the short AWS error CODE stays in the response: it is a
    # bounded enum (SnapshotAlreadyExistsFault vs AccessDenied vs
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
            "create_snapshot failed for %s (rg=%s, snapshot=%s)",
            cluster_id, name, snapshot_name, exc_info=True,
        )
        return {
            "status": "error",
            "reason": (
                f"스냅샷 생성(create_snapshot) 요청이 실패했습니다{code_part}. 대상 이름="
                f"{snapshot_name} (자세한 원인은 서버 로그를 확인하세요). 같은 이름의 "
                "스냅샷이 이미 있거나 클러스터가 available 상태가 아닐 수 있습니다."
            ),
            "cluster_id": cluster_id,
        }
    return {"status": "ok", "cluster_id": cluster_id, "snapshot_name": snapshot_name}
