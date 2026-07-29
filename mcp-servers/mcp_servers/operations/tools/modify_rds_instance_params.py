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
  - The parameter must be MODIFIABLE (describe_db_parameters IsModifiable).
    Also refused before verify_approval, for the same reason: AWS pins a large
    share of every group per engine/version (MEASURED 44 of 117 on
    default.sqlserver-ex-15.0, 149 of 536 on dbops-demo-mysql84), and
    modify_db_parameter_group rejects those. Discovering that AFTER the guard
    consumed the approval burns it on a change that was never possible, which is
    the same defect the aws:ResourceTag condition caused once already (see the
    comment in cdk/cross-account/spoke-role-template.yaml).
  - ApplyMethod is derived from the parameter's own ApplyType: dynamic ->
    immediate, static -> pending-reboot. Forcing pending-reboot on a dynamic
    parameter would make the DBA reboot for a change that needed no downtime;
    claiming a static change took effect without a reboot would be a lie.
  - The parameter GROUP NAME is hash-bound into the approval and compared
    against the group the instance points at RIGHT NOW (TOCTOU): if the instance
    was re-pointed after approval, the change is refused rather than written to a
    group the DBA never saw. That comparison happens BEFORE verify_approval,
    because `live_group` is read in this same invocation and is therefore already
    known: the mismatch used to be found by a SECOND describe_db_instances after
    the consume, which returned state_changed with the approval already gone.
  - CROSS-ACCOUNT AccessDenied is the other precondition still left to the API,
    and it burns the approval when it fires.
    cdk/cross-account/spoke-role-template.yaml (Sid RDSModifyExisting) authorizes
    rds:ModifyDBParameterGroup under `aws:ResourceTag/ManagedBy=dbops`, and that
    action authorizes against the PARAMETER GROUP, not the DB instance, so a
    spoke-account group without the tag is denied AFTER the guard has consumed
    the approval. Same shape as the snapshot-create incident recorded in that
    same template. Not hypothetical: MEASURED on the standing demo fixture, the
    group dbops-demo-mysql84 carries `dbops-demo=true` and `Application=DBOps`
    and NOT ManagedBy, so the day that instance is reached through a spoke role
    the write is denied and the approval is gone.
    It IS knowable: the group's ARN is derivable from the DBInstanceArn this tool
    already reads (`...:db:x` -> `...:pg:<group>`, DRIVEN live) and one
    list_tags_for_resource would answer it. Deliberately NOT done here, for
    three reasons. (1) The condition gates 15 write actions in that template, so
    a check bolted onto this one tool leaves 14 siblings burning approvals while
    looking covered; the fix belongs in one shared pre-consume preflight.
    (2) Refusing on a missing tag asserts what the SPOKE ROLE's policy says, and
    this code does not read that policy: the template is a template customers
    adapt, so an untagged group is not reliably a denial. (3) It must not fire
    same-account, where the hub role has no tag condition at all, so it would
    also need the registry's spoke_role_arn as a second gate.
    The modify_failed reason names the parameter-group tag so a DBA knows where
    to look, and rds:ListTagsForResource is NOT granted to the operations Lambda
    today, so implementing it is an IAM change too.

No raw exception text reaches a return value; details go to the module logger.

ponytail: `AllowedValues` is the one precondition still left to the API. An
out-of-range value is rejected by modify_db_parameter_group AFTER the guard has
consumed the approval, i.e. the same burnt-approval shape as IsModifiable, but
the field is free-form ("0-4294967295", "ON,OFF", enumerations with ranges mixed
in) and a parser that misreads it would refuse writes that are actually legal,
which is worse than one wasted approval. Upgrade path if it becomes worth it:
validate only the two unambiguous shapes (a single "lo-hi" integer range and a
pure comma-separated enumeration) and stay silent on everything else.
"""

import logging

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import client_for_cluster

logger = logging.getLogger(__name__)

# describe_db_parameters / describe_db_cluster_parameters are paginated and have
# no name filter, so the group is scanned for the requested parameter. Bounded on
# purpose: MEASURED 536 parameters over 6 pages for dbops-demo-mysql84, and 448 /
# 416 / 424 over 5 pages each for the Aurora cluster groups pgtsd-demo-cpg,
# default.aurora-postgresql15 and default.aurora-mysql8.0. 25 pages is far past
# any real group while making a bad/never-ending Marker terminate.
_MAX_PARAM_PAGES = 25

_LOOKUP_FAILED = object()


def _describe_instance(rds, cluster_id):
    """Return the instance dict, None when RDS says there is no such instance, or
    _LOOKUP_FAILED when the describe could not be made at all. Never raises.

    Those last two are DIFFERENT answers and this used to collapse them into one,
    which is the same could-not-ask / does-not-exist conflation _find_parameter
    below is explicitly written to avoid. Both landed on not_applicable with a
    reason that told the DBA to check the target identifier: after a throttle or
    an AccessDenied that sends them to fix a name that was never wrong, and the
    identifier is the one thing that is not the problem.
    """
    try:
        instances = rds.describe_db_instances(
            DBInstanceIdentifier=cluster_id).get("DBInstances") or []
    except Exception:
        logger.warning("describe_db_instances failed for %s", cluster_id, exc_info=True)
        return _LOOKUP_FAILED
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


def _find_parameter(describe, group_kwarg, group_name, parameter_name):
    """Locate one parameter in an RDS parameter group.

    `describe` is the bound paginated API and `group_kwarg` its group-name
    keyword, so this serves BOTH forms:
      instance: rds.describe_db_parameters,         "DBParameterGroupName"
      Aurora:   rds.describe_db_cluster_parameters, "DBClusterParameterGroupName"
    The two responses are the same shape (Parameters[] + Marker) and carry the
    same per-parameter fields, MEASURED on both, so one scan covers both. The
    Aurora CLUSTER tool used to call NEITHER describe, which is why it could see
    neither IsModifiable nor whether the parameter existed.

    Returns the parameter's OWN dict, None when the group says it does not have
    it, or _LOOKUP_FAILED when the group could not be read at all. The caller
    MUST distinguish "the group says this parameter does not exist" from "we
    could not ask", because only the first is a safe refusal.

    The whole dict is returned on purpose. This used to project three fields out
    of it and drop the rest, and the field it dropped was `IsModifiable`, i.e.
    exactly the precondition that decides whether the write can succeed at all.

    Matching is CASE-INSENSITIVE. RDS reports SQL Server parameter names in LOWER
    case ('max server memory (mb)', 'agent xps'), while sys.configurations, which
    is what sp_configure and this product's Configuration tab show, spells the
    same options in mixed case ('max server memory (MB)', 'Agent XPs'). A
    case-sensitive compare therefore rejected 7 of the 23 option names the
    dashboard itself displays. Folding case cannot merge two distinct parameters:
    MEASURED zero names differing only by case in default.sqlserver-ex-15.0
    (117), dbops-demo-mysql84 (536), pgtsd-demo-cpg (448),
    default.aurora-postgresql15 (416) and default.aurora-mysql8.0 (424).

    Whether the CALLER then adopts the API's spelling is the caller's decision:
    this instance tool does (its approval projection case-folds parameter_name to
    match), the Aurora tool does NOT (its projection does not fold, so rewriting
    the name would break an in-flight approval hash, and MEASURED zero Aurora
    parameter names are mixed-case, so there is nothing to reconcile).
    """
    wanted = parameter_name.strip().lower()
    marker = None
    for _ in range(_MAX_PARAM_PAGES):
        kwargs = {group_kwarg: group_name}
        if marker:
            kwargs["Marker"] = marker
        try:
            resp = describe(**kwargs)
        except Exception:
            logger.warning("%s failed for %s", getattr(describe, "__name__", "describe"),
                           group_name, exc_info=True)
            return _LOOKUP_FAILED
        for p in resp.get("Parameters") or []:
            if str(p.get("ParameterName") or "").strip().lower() == wanted:
                return p
        marker = resp.get("Marker")
        # A non-str marker (or a falsy one) ends the scan. Without the isinstance
        # check a test double handing back a truthy mock would loop forever.
        if not isinstance(marker, str) or not marker:
            return None
    logger.warning("parameter scan exceeded %d pages for %s",
                   _MAX_PARAM_PAGES, group_name)
    return _LOOKUP_FAILED


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
    if inst is _LOOKUP_FAILED:
        # We could not ASK. Reusing lookup_failed, the status the parameter scan
        # below already returns for the same class of answer.
        return {"status": "lookup_failed", "cluster_id": cluster_id,
                "reason": "인스턴스 정보를 조회할 수 없어 변경하지 않았습니다. 잠시 후 다시 "
                          "시도하세요 (자세한 원인은 서버 로그를 확인하세요)."}
    if inst is None:
        # RDS answered, and the answer is that this instance does not exist. Only
        # here is "check the identifier" actionable advice.
        return {"status": "not_applicable", "cluster_id": cluster_id,
                "reason": "해당 식별자의 RDS 인스턴스를 찾을 수 없습니다. 대상 식별자를 "
                          "확인하세요."}

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

    found = _find_parameter(rds.describe_db_parameters, "DBParameterGroupName",
                            live_group, parameter_name)
    if found is _LOOKUP_FAILED:
        return {"status": "lookup_failed", "cluster_id": cluster_id,
                "parameter_group": live_group,
                "reason": "파라미터 그룹의 파라미터 목록을 조회할 수 없어 변경하지 않았습니다 "
                          "(자세한 원인은 서버 로그를 확인하세요)."}
    if found is None:
        return {"status": "unknown_parameter", "cluster_id": cluster_id,
                "parameter_group": live_group, "parameter": parameter_name,
                "reason": f"파라미터 그룹 '{live_group}'에 {parameter_name!r} 파라미터가 "
                          "없습니다. 엔진/버전에 맞는 이름인지 확인하세요."}

    # From here on the API's spelling is the parameter's name: it goes into the
    # responses, into the approval payload and into the write, so the name the
    # DBA reads back is the name that was actually sent to AWS.
    parameter_name = str(found.get("ParameterName") or "").strip() or parameter_name
    # ParameterValue is ABSENT for a parameter left at the engine default, which
    # is not the same as an empty string.
    current_value = found.get("ParameterValue")
    apply_type = found.get("ApplyType") or ""

    # AWS pins part of every group per engine/version. modify_db_parameter_group
    # REFUSES those, so this has to be answered before verify_approval: the
    # approval is single-use, and consuming it for a change the API was always
    # going to reject burns it (the retry then dies with "already consumed").
    # Only an explicit False refuses: a response that does not carry the field
    # has not told us the parameter is fixed, and claiming it did would be a
    # negative the data does not support.
    if found.get("IsModifiable") is False:
        return {
            "status": "not_modifiable",
            "cluster_id": cluster_id,
            "parameter_group": live_group,
            "parameter": parameter_name,
            "current_value": current_value,
            "apply_type": apply_type,
            "reason": (
                f"'{parameter_name}' 파라미터는 이 파라미터 그룹에서 수정할 수 없습니다"
                f"(describe_db_parameters의 IsModifiable=false). 엔진/버전 단위로 AWS가 "
                f"고정한 값이라 변경 요청 자체가 거부되므로, 승인을 소모하지 않고 여기서 "
                f"중단합니다. 현재 값은 "
                f"{(current_value if current_value is not None else '엔진 기본값')!r}입니다. "
                f"수정 가능한 다른 파라미터를 쓰거나, 엔진 버전 업그레이드가 필요한지 "
                f"확인하세요."
            ),
        }

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

    # TOCTOU, answered with what is ALREADY IN HAND. `live_group` is the group
    # the instance points at right now, read a few lines up in THIS invocation,
    # and `parameter_group` is the group the DBA's approval is hash-bound to. So
    # the comparison belongs here, BEFORE the guard consumes the approval.
    #
    # This used to run AFTER the consume, off a SECOND describe_db_instances, and
    # returned state_changed with the approval already gone: a drift that was
    # visible before the guard cost the DBA the approval AND the change. The
    # second describe is gone rather than merely reordered, because it could not
    # observe anything the first one did not: both run at execute time, so the
    # window it covered is the microseconds between two adjacent calls, not the
    # minutes between approval and execute (which is what `parameter_group` being
    # in the hash covers). Should the instance be re-pointed inside that
    # microsecond window, the write still lands on the group named on the approval
    # card, i.e. the group the DBA reviewed, which is the correct target.
    #
    # An empty/omitted `parameter_group` lands here too, and the reason says
    # "different from", not "changed": the arg may simply have been left out, and
    # asserting drift would be a claim the data does not support. Either way the
    # approval survives (it previously failed the hash instead, which was also
    # closed but reported as a payload mismatch).
    if live_group != parameter_group:
        return {"status": "state_changed", "cluster_id": cluster_id,
                "parameter_group": live_group,
                "approved_parameter_group": parameter_group,
                "reason": (
                    f"승인에 기록된 DB 파라미터 그룹({parameter_group!r})이 이 인스턴스가 "
                    f"현재 사용하는 그룹({live_group!r})과 다릅니다. 승인 이후 그룹이 "
                    f"바뀌었을 수 있습니다. 승인을 소모하지 않았으니, 현재 그룹으로 다시 "
                    f"승인 요청하세요."
                )}

    guard = verify_approval(
        approval_id, cluster_id, "modify_rds_instance_params",
        payload={"cluster_id": cluster_id, "parameter_name": parameter_name,
                 "value": value, "parameter_group": parameter_group},
    )
    if not guard.get("ok"):
        return {"status": "approval_denied", "cluster_id": cluster_id,
                "parameter": parameter_name, "value": value,
                "reason": guard.get("reason", "approval guard rejected the request")}

    try:
        rds.modify_db_parameter_group(
            DBParameterGroupName=live_group,
            Parameters=[{
                "ParameterName": parameter_name,
                "ParameterValue": value,
                "ApplyMethod": apply_method,
            }],
        )
    except Exception:
        logger.warning(
            "modify_db_parameter_group failed for %s (group=%s, param=%s)",
            cluster_id, live_group, parameter_name, exc_info=True,
        )
        return {"status": "modify_failed", "cluster_id": cluster_id,
                "parameter_group": live_group, "parameter": parameter_name,
                "reason": "DB 파라미터 그룹 수정에 실패했습니다 (값 유효 범위·권한·파라미터 "
                          "그룹 태그를 확인하세요)."}

    return {
        "status": "modified",
        "cluster_id": cluster_id,
        "parameter_group": live_group,
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
            f"DB 파라미터 그룹 '{live_group}'에 {parameter_name}={value}로 등록했습니다. "
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
