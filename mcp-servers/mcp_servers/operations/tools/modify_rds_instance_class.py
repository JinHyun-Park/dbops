"""modify_rds_instance_class — approval-gated compute resize of a STANDALONE RDS
DB instance (non-Aurora: MySQL / SQL Server, the rds_instance engine family; R-3).

The handler positive-gates this tool on the rds_instance-only `instance_write`
capability (FAIL-CLOSED), so any other engine — or an unresolvable cluster —
gets unsupported_engine before the impl runs.

The instance's CURRENT class is read in preview and hash-bound into the approval
alongside the target. Execute re-reads it on a FRESH describe (TOCTOU): if the
class drifted since approval (someone else resized), the change is refused rather
than applied on top of an unexpected state. modify_db_instance runs with
ApplyImmediately=True.

FAIL-CLOSED: verify_approval must pass; no str(e) internals leak into a return.
"""

import logging

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster
from mcp_servers.shared.managed_tag_preflight import resource_tag_warning

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


def modify_rds_instance_class_impl(
    cache: CacheClient,
    cluster_id: str,
    target_class: str = "",
    current_class: str = "",
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    target_class = (target_class or "").strip()
    current_class = (current_class or "").strip()

    if not target_class:
        return {"status": "invalid_request", "cluster_id": cluster_id,
                "reason": "target_class가 필요합니다 (예: db.r6g.large)."}

    rds = client_for_cluster(cluster_id, "rds")

    if not approved:
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
        if inst.get("DBInstanceStatus") != "available":
            return {"status": "not_applicable", "cluster_id": cluster_id,
                    "reason": f"인스턴스 상태가 available이 아닙니다 (현재: {inst.get('DBInstanceStatus')})."}
        live_class = inst.get("DBInstanceClass") or ""
        if live_class == target_class:
            return {"status": "not_applicable", "cluster_id": cluster_id,
                    "reason": f"인스턴스가 이미 {target_class!r} 클래스입니다 (변경 없음)."}
        # Bind the current class NOW so the approval hash pins the baseline
        # state; execute refuses if the live class has drifted from it.
        card = {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "target_class": target_class,
            "current_class": live_class,
            "cli_preview": (
                f"RDS 인스턴스 클래스 변경: {cluster_id!r} 인스턴스를 "
                f"{live_class!r} → {target_class!r}로 변경합니다 (ApplyImmediately). "
                "인스턴스 재기동으로 짧은 다운타임이 발생하며 시간당 비용이 달라집니다."
            ),
        }
        # The instance ARN is already in the describe above, so the cross-account
        # tag costs one list-tags call and no extra describe. WARNING, never a
        # refusal: see managed_tag_preflight.
        tag_warning = resource_tag_warning(
            rds.list_tags_for_resource, inst.get("DBInstanceArn"), cluster_id,
            label="DB 인스턴스", action="rds:ModifyDBInstance")
        if tag_warning:
            card["warning"] = tag_warning
        return card

    guard = verify_approval(
        approval_id, cluster_id, "modify_rds_instance_class",
        payload={"cluster_id": cluster_id, "target_class": target_class,
                 "current_class": current_class},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "reason": guard.get("reason", "approval guard rejected the request")}

    # TOCTOU: re-read the live class and refuse if it drifted from the class the
    # approval was bound to — never resize on top of an unexpected state.
    fresh = _describe(rds, cluster_id)
    if fresh is _LOOKUP_FAILED:
        # The approval is SINGLE-USE and was consumed above, so say so: this is the
        # one place the DBA has to re-request, and a message that only says
        # "state changed" points them at a state that may not have changed at all.
        return {"status": "lookup_failed", "cluster_id": cluster_id,
                "reason": ("변경 직전 재확인을 위한 RDS describe 호출이 실패해 안전을 위해 "
                           "중단했습니다 (throttling 또는 권한 문제일 수 있습니다). "
                           "이 승인은 이미 소진되었으므로 변경이 필요하면 승인을 다시 "
                           "요청해야 합니다.")}
    if fresh is None:
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": ("승인 이후 해당 인스턴스가 더 이상 존재하지 않습니다. 변경하지 "
                           "않았으며, 이 승인은 이미 소진되었습니다.")}
    if fresh.get("DBInstanceStatus") != "available":
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": (f"승인 이후 인스턴스 상태가 바뀌었습니다 (현재: "
                           f"{fresh.get('DBInstanceStatus')}). 변경하지 않았습니다.")}
    if (fresh.get("DBInstanceClass") or "") != current_class:
        return {"status": "state_changed", "cluster_id": cluster_id,
                "reason": ("승인 이후 인스턴스 클래스가 변경되었습니다. 안전을 위해 변경하지 "
                           "않았습니다. 다시 승인 요청하세요.")}

    try:
        rds.modify_db_instance(
            DBInstanceIdentifier=cluster_id, DBInstanceClass=target_class,
            ApplyImmediately=True)
    except Exception:
        logger.warning("modify_db_instance failed for %s", cluster_id, exc_info=True)
        return {"status": "modify_failed", "cluster_id": cluster_id,
                "reason": "인스턴스 클래스 변경에 실패했습니다 (클래스 유효성·상태·권한 확인)."}

    return {"status": "modifying", "cluster_id": cluster_id,
            "target_class": target_class, "db_status": "modifying"}
