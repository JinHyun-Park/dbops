"""modify_rds_instance_params: approval-gated INSTANCE parameter-group change for
a standalone RDS DB instance (non-Aurora MySQL / SQL Server; the rds_instance
family, E-3).

Why a separate tool from modify_parameter: that tool calls
`describe_db_clusters` -> `modify_db_cluster_parameter_group`, i.e. the Aurora
CLUSTER parameter group, which does not exist for a standalone DB instance. Every
parameter read/write site in this repo used the CLUSTER form before E-3 (there
was not one `describe_db_parameters` call anywhere), so this is a new path, not a
widened one. The Aurora cluster tool keeps refusing this family, and this tool
refuses Aurora, both through the same positive, FAIL-CLOSED capability gate.

Gate: the handler positive-gates on the rds_instance-only `instance_write`
capability. No new capability key was added: `instance_write` already means
exactly "a write that applies to a standalone RDS DB instance", which is the
predicate here. An Aurora cluster, any non-relational family, and any cluster
whose engine cannot be resolved all get unsupported_engine before this impl runs.

Safety:
  - `default.*` groups are AWS-managed and IMMUTABLE. Refused BEFORE
    verify_approval, so a refusal on a precondition that was already true at
    approval time can never burn the single-use approval.
  - The parameter must EXIST in the group's family, read from
    describe_db_parameters. Modifying a name the family does not have would be
    accepted by the API into a group nothing reads.
  - ApplyMethod is derived from the parameter's own ApplyType: dynamic ->
    immediate, static -> pending-reboot. Forcing pending-reboot on a dynamic
    parameter would make the DBA reboot for a change that needed no downtime;
    claiming a static change took effect without a reboot would be a lie.
  - The parameter GROUP NAME is hash-bound into the approval and re-read on a
    FRESH describe at execute (TOCTOU): if the instance was pointed at a
    different group after approval, the change is refused rather than written to
    a group the DBA never saw.

No raw exception text reaches a return value; details go to the module logger.
"""

import logging

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster

logger = logging.getLogger(__name__)

# describe_db_parameters is paginated and has no name filter, so the group is
# scanned for the requested parameter. Bounded on purpose: RDS MySQL 8.x carries
# roughly 500 parameters (about 5 pages at the default MaxRecords), so 25 pages
# is far past any real group while making a bad/never-ending Marker terminate.
_MAX_PARAM_PAGES = 25


def _describe_instance(rds, cluster_id):
    """Return the instance dict, or None. Never raises."""
    try:
        instances = rds.describe_db_instances(
            DBInstanceIdentifier=cluster_id).get("DBInstances") or []
    except Exception:
        logger.warning("describe_db_instances failed for %s", cluster_id, exc_info=True)
        return None
    return instances[0] if instances else None


def _instance_param_group(inst):
    """The instance's DB parameter group name, or "".

    RDS reports one entry per instance in DBParameterGroups; take the first and
    do not guess a name when the list is empty.
    """
    groups = (inst or {}).get("DBParameterGroups") or []
    if not groups:
        return ""
    return groups[0].get("DBParameterGroupName") or ""


def _find_parameter(rds, group_name, parameter_name):
    """Locate one parameter in a DB parameter group.

    Returns (found, current_value, apply_type) where found is True/False, or
    (None, None, None) when the group could not be read at all. The caller MUST
    distinguish "the group says this parameter does not exist" from "we could not
    ask", because only the first is a safe refusal.
    """
    marker = None
    for _ in range(_MAX_PARAM_PAGES):
        kwargs = {"DBParameterGroupName": group_name}
        if marker:
            kwargs["Marker"] = marker
        try:
            resp = rds.describe_db_parameters(**kwargs)
        except Exception:
            logger.warning("describe_db_parameters failed for %s", group_name, exc_info=True)
            return None, None, None
        for p in resp.get("Parameters") or []:
            if p.get("ParameterName") == parameter_name:
                # ParameterValue is ABSENT for a parameter left at the engine
                # default, which is not the same as an empty string.
                return True, p.get("ParameterValue"), (p.get("ApplyType") or "")
        marker = resp.get("Marker")
        # A non-str marker (or a falsy one) ends the scan. Without the isinstance
        # check a test double handing back a truthy mock would loop forever.
        if not isinstance(marker, str) or not marker:
            return False, None, None
    logger.warning("describe_db_parameters exceeded %d pages for %s",
                   _MAX_PARAM_PAGES, group_name)
    return None, None, None


def modify_rds_instance_params_impl(
    cache: CacheClient,
    cluster_id: str,
    parameter_name: str = "",
    value: str = "",
    parameter_group: str = "",
    approved: bool = False,
    approval_id: str = "",
    **_ignored,
) -> dict:
    parameter_name = (parameter_name or "").strip()
    # Only surrounding whitespace goes: "0" and "OFF" are legitimate values. The
    # same str().strip() runs in approval_guard._project, so the preview and the
    # execute hash the same text. An empty value is refused rather than sent as a
    # reset to the engine default, which is a different operation.
    value = str(value if value is not None else "").strip()
    parameter_group = (parameter_group or "").strip()

    if not parameter_name:
        return {"status": "invalid_request", "cluster_id": cluster_id,
                "reason": "parameter_name이 필요합니다 (예: innodb_buffer_pool_size)."}
    if not value:
        return {"status": "invalid_request", "cluster_id": cluster_id,
                "reason": "value가 필요합니다. 파라미터를 엔진 기본값으로 되돌리는 것은 "
                          "이 툴이 지원하지 않는 별개의 작업입니다."}

    rds = client_for_cluster(cluster_id, "rds")

    inst = _describe_instance(rds, cluster_id)
    if inst is None:
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": "인스턴스를 조회할 수 없습니다. 대상 식별자를 확인하세요."}

    live_group = _instance_param_group(inst)
    if not live_group:
        return {"status": "no_parameter_group", "cluster_id": cluster_id,
                "reason": "인스턴스에 연결된 DB 파라미터 그룹을 찾을 수 없습니다."}

    # AWS-managed default group: immutable. A custom group has to be created and
    # attached first, which is its own (reboot-requiring) workflow.
    if live_group.startswith("default."):
        return {
            "status": "default_group_refused",
            "cluster_id": cluster_id,
            "parameter_group": live_group,
            "reason": (
                f"이 인스턴스는 AWS 기본 파라미터 그룹 '{live_group}'을 사용합니다. "
                "기본 그룹은 수정할 수 없으므로, 먼저 커스텀 DB 파라미터 그룹을 만들어 "
                "인스턴스에 연결(재시작 필요)한 뒤 다시 요청하세요."
            ),
        }

    found, current_value, apply_type = _find_parameter(rds, live_group, parameter_name)
    if found is None:
        return {"status": "lookup_failed", "cluster_id": cluster_id,
                "parameter_group": live_group,
                "reason": "파라미터 그룹의 파라미터 목록을 조회할 수 없어 변경하지 않았습니다 "
                          "(자세한 원인은 서버 로그를 확인하세요)."}
    if not found:
        return {"status": "unknown_parameter", "cluster_id": cluster_id,
                "parameter_group": live_group, "parameter": parameter_name,
                "reason": f"파라미터 그룹 '{live_group}'에 {parameter_name!r} 파라미터가 "
                          "없습니다. 엔진/버전에 맞는 이름인지 확인하세요."}

    # dynamic -> immediate (no restart), static -> pending-reboot. Anything else
    # (an ApplyType this code has not seen) takes the conservative branch.
    is_dynamic = apply_type.lower() == "dynamic"
    apply_method = "immediate" if is_dynamic else "pending-reboot"

    if not approved:
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "parameter": parameter_name,
            "value": value,
            # Bound into the approval hash: execute refuses if the instance is
            # pointed at a different group after approval.
            "parameter_group": live_group,
            "current_value": current_value,
            "apply_type": apply_type,
            "apply_method": apply_method,
            "cli_preview": (
                f"RDS 인스턴스 파라미터 변경: 인스턴스 {cluster_id!r}의 DB 파라미터 그룹 "
                f"{live_group!r}에서 {parameter_name}을 "
                f"{(current_value if current_value is not None else '엔진 기본값')!r} → "
                f"{value!r}로 변경합니다. "
                + (
                    "이 파라미터는 dynamic이라 ApplyMethod=immediate로 즉시 반영됩니다"
                    if is_dynamic else
                    "이 파라미터는 static이라 ApplyMethod=pending-reboot로 등록되며 "
                    "**인스턴스 재시작 후에** 동작값이 바뀝니다"
                )
                + ". 같은 파라미터 그룹을 쓰는 다른 인스턴스에도 함께 적용됩니다."
            ),
        }

    guard = verify_approval(
        approval_id, cluster_id, "modify_rds_instance_params",
        payload={"cluster_id": cluster_id, "parameter_name": parameter_name,
                 "value": value, "parameter_group": parameter_group},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "parameter": parameter_name, "value": value,
                "reason": guard.get("reason", "approval guard rejected the request")}

    # TOCTOU: the approval pinned a group name; re-read it and refuse if the
    # instance now points somewhere else. Writing to the approved-but-detached
    # group would change an instance the DBA never reviewed.
    fresh = _describe_instance(rds, cluster_id)
    fresh_group = _instance_param_group(fresh)
    if not fresh_group:
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": "승인 이후 인스턴스의 파라미터 그룹을 확인할 수 없어 변경하지 않았습니다."}
    if fresh_group != parameter_group:
        return {"status": "state_changed", "cluster_id": cluster_id,
                "parameter_group": fresh_group,
                "reason": "승인 이후 인스턴스의 DB 파라미터 그룹이 바뀌었습니다. 안전을 위해 "
                          "변경하지 않았습니다. 다시 승인 요청하세요."}
    if fresh_group.startswith("default."):
        return {"status": "default_group_refused", "cluster_id": cluster_id,
                "parameter_group": fresh_group,
                "reason": "승인 이후 인스턴스가 AWS 기본 파라미터 그룹으로 바뀌었습니다. "
                          "기본 그룹은 수정할 수 없습니다."}

    try:
        rds.modify_db_parameter_group(
            DBParameterGroupName=fresh_group,
            Parameters=[{
                "ParameterName": parameter_name,
                "ParameterValue": value,
                "ApplyMethod": apply_method,
            }],
        )
    except Exception:
        logger.warning(
            "modify_db_parameter_group failed for %s (group=%s, param=%s)",
            cluster_id, fresh_group, parameter_name, exc_info=True,
        )
        return {"status": "modify_failed", "cluster_id": cluster_id,
                "parameter_group": fresh_group, "parameter": parameter_name,
                "reason": "DB 파라미터 그룹 수정에 실패했습니다 (값 유효 범위·권한·파라미터 "
                          "그룹 태그를 확인하세요)."}

    return {
        "status": "modified",
        "cluster_id": cluster_id,
        "parameter_group": fresh_group,
        "parameter": parameter_name,
        "value": value,
        "previous_value": current_value,
        "apply_type": apply_type,
        "apply_method": apply_method,
        # applied: has the RUNNING value changed yet. immediate -> yes, pending-
        # reboot -> no. Reporting True for a static parameter is the "approved but
        # nothing changed" confusion this field exists to prevent.
        "applied": is_dynamic,
        "note": (
            f"DB 파라미터 그룹 '{fresh_group}'에 {parameter_name}={value}로 등록했습니다. "
            + (
                "ApplyMethod=immediate이라 동작값에 즉시 반영됩니다. "
                if is_dynamic else
                "ApplyMethod=pending-reboot이라 **실제 적용에는 인스턴스 재시작이 필요**합니다. "
                "재시작 전까지 동작값은 바뀌지 않습니다. 재시작 시점은 유지보수 윈도우 또는 "
                "reboot_rds_instance로 계획하세요. "
            )
            + "대시보드의 Configuration은 5분 주기 수집 캐시라 반영까지 최대 5분 걸립니다. "
            "이 파라미터 그룹을 공유하는 다른 인스턴스에도 같은 변경이 적용됩니다."
        ),
    }
