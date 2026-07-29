"""reboot_rds_instance — approval-gated reboot of a STANDALONE RDS DB instance
(non-Aurora: MySQL / SQL Server, the rds_instance engine family; R-3).

The handler positive-gates this tool on the rds_instance-only `instance_write`
capability (FAIL-CLOSED), so any other engine — or an unresolvable cluster —
gets unsupported_engine before the impl runs.

SAFETY: an Aurora cluster member (the instance carries a DBClusterIdentifier) is
refused — Aurora reboots go through cluster/reader tooling, never this
instance-level tool.

FAIL-CLOSED like every write tool: verify_approval must pass before the reboot,
a FRESH describe re-checks state right before the call (TOCTOU), and no str(e)
internals leak into a return shown to users (errors are logged to CloudWatch).
"""

import logging

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster

logger = logging.getLogger(__name__)

# COULD-NOT-ASK, as distinct from DOES-NOT-EXIST. See _describe.
_LOOKUP_FAILED = object()


def _describe(rds, cluster_id):
    """Return the instance dict, None when RDS says there is no such instance, or
    _LOOKUP_FAILED when the describe could not be made at all. Never raises.

    Those last two are DIFFERENT answers, and collapsing them into one None sent
    the DBA to check the target identifier after a throttle or an AccessDenied,
    where the identifier is the one thing that is not the problem. Same split
    modify_rds_instance_params already ships.
    """
    try:
        instances = rds.describe_db_instances(DBInstanceIdentifier=cluster_id).get(
            "DBInstances"
        ) or []
    except Exception:
        logger.warning("describe_db_instances failed for %s", cluster_id, exc_info=True)
        return _LOOKUP_FAILED
    return instances[0] if instances else None


def reboot_rds_instance_impl(
    cache: CacheClient,
    cluster_id: str,
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    rds = client_for_cluster(cluster_id, "rds")

    # Pre-check BEFORE offering approval: skip the approval round-trip for an
    # instance that can't be rebooted (not available / is an Aurora member).
    inst = _describe(rds, cluster_id)
    if inst is _LOOKUP_FAILED:
        return {"status": "lookup_failed", "cluster_id": cluster_id,
                "reason": ("RDS describe 호출 자체가 실패해 인스턴스 상태를 확인하지 "
                           "못했습니다 (throttling 또는 권한 문제일 수 있습니다). "
                           "대상 식별자 문제가 아니므로 잠시 후 다시 시도하고, 반복되면 "
                           "IAM 권한을 확인하세요. 승인은 아직 요청되지 않았습니다.")}
    if inst is None:
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": "해당 식별자의 RDS 인스턴스가 존재하지 않습니다. 대상 식별자를 확인하세요."}
    if inst.get("DBClusterIdentifier"):
        return {"status": "unsupported", "cluster_id": cluster_id,
                "reason": "Aurora 클러스터 멤버는 이 툴로 재부팅할 수 없습니다 (클러스터 도구를 사용하세요)."}
    if inst.get("DBInstanceStatus") != "available":
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": f"인스턴스 상태가 available이 아닙니다 (현재: {inst.get('DBInstanceStatus')})."}

    if not approved:
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "cli_preview": (
                f"RDS 인스턴스 재부팅: {cluster_id!r} 인스턴스를 재부팅합니다. "
                "재부팅 동안 짧은 다운타임이 발생하며, Multi-AZ가 아니면 연결이 끊깁니다."
            ),
        }

    guard = verify_approval(
        approval_id, cluster_id, "reboot_rds_instance",
        payload={"cluster_id": cluster_id},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    # TOCTOU: re-check on a FRESH describe immediately before the reboot — the
    # instance may have left `available` (or become a cluster member) in the
    # window since approval.
    fresh = _describe(rds, cluster_id)
    if fresh is _LOOKUP_FAILED:
        # The approval is SINGLE-USE and was consumed by verify_approval above, so
        # say so: this is the one place the DBA has to re-request, and a message
        # that only says "aborted" leaves them re-issuing the same call forever.
        return {"status": "lookup_failed", "cluster_id": cluster_id,
                "reason": ("재부팅 직전 재확인을 위한 RDS describe 호출이 실패해 안전을 위해 "
                           "중단했습니다 (throttling 또는 권한 문제일 수 있습니다). "
                           "이 승인은 이미 소진되었으므로 재부팅이 필요하면 승인을 다시 "
                           "요청해야 합니다.")}
    if fresh is None:
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": ("승인 이후 해당 인스턴스가 더 이상 존재하지 않습니다. 재부팅하지 "
                           "않았으며, 이 승인은 이미 소진되었습니다.")}
    if fresh.get("DBClusterIdentifier"):
        return {"status": "unsupported", "cluster_id": cluster_id,
                "reason": "Aurora 클러스터 멤버는 이 툴로 재부팅할 수 없습니다 (클러스터 도구를 사용하세요)."}
    if fresh.get("DBInstanceStatus") != "available":
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": f"승인 이후 인스턴스 상태가 바뀌었습니다 (현재: {fresh.get('DBInstanceStatus')}) — 재부팅하지 않았습니다."}

    try:
        rds.reboot_db_instance(DBInstanceIdentifier=cluster_id)
    except Exception:
        logger.warning("reboot_db_instance failed for %s", cluster_id, exc_info=True)
        return {"status": "reboot_failed", "cluster_id": cluster_id,
                "reason": "인스턴스 재부팅에 실패했습니다 (상태·권한 확인)."}

    return {"status": "rebooting", "cluster_id": cluster_id, "db_status": "rebooting"}
