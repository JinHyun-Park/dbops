"""modify_parameter: approval-gated Aurora CLUSTER parameter-group change.

Aurora-relational only. The handler positive-gates this tool on the
`cluster_parameter` capability (FAIL-CLOSED), so rds_instance (which has
INSTANCE parameter groups), DocumentDB, DynamoDB and ElastiCache refuse with
unsupported_engine instead of failing inside `rds.describe_db_clusters`.

THE APPROVAL IS SINGLE-USE. Every precondition this tool can answer from the two
describes it already makes, plus the shape of its own arguments, is answered
before `verify_approval`. TWO are deliberately left to the API and both burn the
approval when they fire (`AllowedValues` and the cross-account tag, see below), so
"everything knowable" would be an overclaim: what holds is that nothing this code
can see is discovered after the consume. This tool used to go straight from
describe_db_clusters to modify_db_cluster_parameter_group with no
describe_db_cluster_parameters anywhere, so it could see NEITHER whether the
parameter is modifiable NOR whether it exists in the group at all, and both
refusals therefore arrived from AWS only after the guard had consumed the DBA's
approval. MEASURED pre-fix against the live cluster pgtsd-demo-aurora-pg (custom
group pgtsd-demo-cpg): `config_file` (IsModifiable=false) and a nonexistent name
both returned modify_failed with verify_approval called ONCE, i.e. the approval
gone, and the write actually attempted. Not a corner case: 39 of the 448
parameters in pgtsd-demo-cpg, 34 of 416 in default.aurora-postgresql15 and 65 of
424 in default.aurora-mysql8.0 are pinned by AWS.

The parameter lookup is the SAME FUNCTION the INSTANCE form uses
(modify_rds_instance_params._find_parameter), not a copy of it: the two describe
APIs return the same paginated shape with the same per-parameter fields, so only
the bound API and its group keyword are passed in. Sibling tools in this package
already share helpers this way (modify_custom_endpoint imports
delete_custom_endpoint.find_custom_endpoint).

Because that lookup matches on `.strip().lower()`, the arguments are normalised
the same way BEFORE anything is bound to the approval, and from the moment the
parameter is found the API's OWN spelling is the name (response, approval payload
and write alike). Sharing the case-insensitive lookup without that made the
preflight pass on a padded or mis-cased name and then hand the CALLER's raw string
to modify_db_cluster_parameter_group, i.e. it moved the burn from the preflight to
the write instead of removing it. MEASURED pre-fix with verify_approval spied and
the write intercepted by a local double: `" max_connections "`, `MAX_CONNECTIONS`
and an empty value all reached the write with verify_approval called ONCE (the
approval gone). Post-fix the first two are normalised and the write receives
`max_connections`; the empty value is refused as invalid_request with 0 consumes,
because clearing a parameter back to the engine default is
reset_db_cluster_parameter_group, a different operation this tool does not do.
`approval_guard._project("modify_parameter")` folds parameter_name and strips
value for the same reason the instance action does: adopting the API's spelling on
only one leg would leave a card that can never verify, which fails closed but into
a loop the DBA cannot exit.

THE GROUP IS PART OF WHAT WAS APPROVED. `parameter_group` is bound into the
approval payload and compared against the cluster's live group BEFORE the guard
runs, mirroring the instance tool. It used to be informational only: the
projection bound {parameter_name, value}, so a cluster re-pointed to a DIFFERENT
cluster parameter group between the approval and the execute had the write land on
the new group while the card named none. Same parameter, same value, wrong target,
and nothing in the audit record said so. A mismatch now returns state_changed
WITHOUT consuming the approval.

Still left to the API, exactly as in the instance tool, and both burn the approval
when they fire: `AllowedValues` (the field is free-form, and a parser that
misreads it would refuse legal writes) and the cross-account
`aws:ResourceTag/ManagedBy=dbops` condition, which for
rds:ModifyDBClusterParameterGroup authorizes against the CLUSTER PARAMETER GROUP
rather than the cluster, so an untagged spoke-account group is denied after the
consume. See the safety notes in modify_rds_instance_params for why that one is
recorded rather than checked: it gates 15 write actions in the spoke template and
belongs in one shared preflight, not in each tool.

Failures return a STATIC Korean reason and log the detail with the module
logger: raw exception text must never reach a tool response.
"""

import logging

from mcp_servers.operations.tools.modify_rds_instance_params import (
    _LOOKUP_FAILED,
    _find_parameter,
)
from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster

logger = logging.getLogger(__name__)


def modify_parameter_impl(
    cache: CacheClient,
    cluster_id: str,
    parameter_name: str,
    value: str,
    parameter_group: str = "",
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    # Every static refusal happens BEFORE verify_approval (same ordering as
    # set_docdb_profiler). The approval is SINGLE-USE: consuming it and then
    # refusing on a precondition that was already true at approval time burnt the
    # approval, and the retry died with "already consumed".
    #
    # The ARGUMENTS are a precondition too, and the cheapest one. Same
    # normalisation as modify_rds_instance_params: only surrounding whitespace
    # goes, because "0" and "off" are legitimate parameter values, and
    # _project("modify_parameter") folds the name and strips the value identically
    # so the preview leg and the execute leg hash the same text.
    parameter_name = (parameter_name or "").strip()
    value = str(value if value is not None else "").strip()
    parameter_group = (parameter_group or "").strip()

    if not parameter_name:
        return {"status": "invalid_request", "cluster_id": cluster_id,
                "reason": "parameter_name이 필요합니다 (예: work_mem)."}
    if not value:
        return {"status": "invalid_request", "cluster_id": cluster_id,
                "reason": "value가 필요합니다. 파라미터를 엔진 기본값으로 되돌리는 것은 "
                          "이 툴이 지원하지 않는 별개의 작업입니다"
                          "(reset_db_cluster_parameter_group)."}

    try:
        rds = rds_client_for_cluster(cluster_id)
    except Exception:
        logger.warning("rds client init failed for %s", cluster_id, exc_info=True)
        return {
            "status": "error",
            "cluster_id": cluster_id,
            "reason": "RDS 제어 플레인 클라이언트를 만들 수 없습니다 (자세한 원인은 서버 로그를 확인하세요).",
        }

    # Look up the cluster's actual parameter group rather than guessing a name.
    try:
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = (resp.get("DBClusters") or [{}])[0]
    except Exception:
        logger.warning("describe_db_clusters failed for %s", cluster_id, exc_info=True)
        return {
            "status": "lookup_failed",
            "cluster_id": cluster_id,
            "reason": "클러스터의 파라미터 그룹을 조회할 수 없습니다 (자세한 원인은 서버 로그를 확인하세요).",
        }

    pg_name = cluster.get("DBClusterParameterGroup") or ""
    if not pg_name:
        return {"status": "no_parameter_group", "cluster_id": cluster_id}

    # AWS-managed `default.*` cluster parameter group: IMMUTABLE, exactly like the
    # instance-form default groups. Mirrors modify_rds_instance_params, whose
    # refusal names the workflow that unblocks it instead of just the fact. The
    # message used to be a bare English sentence; the reason a DBA reads has to
    # tell them what to do next. MEASURED live: 5 of the 6 Aurora clusters in this
    # account sit on a default.* cluster group, so this is the common case.
    if pg_name.startswith("default."):
        return {
            "status": "default_group_refused",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "reason": (
                f"이 클러스터는 AWS 기본 클러스터 파라미터 그룹 '{pg_name}'을 사용합니다. "
                "기본 그룹은 수정할 수 없으므로, 먼저 커스텀 클러스터 파라미터 그룹을 만들어 "
                "클러스터에 연결(인스턴스 재시작 필요)한 뒤 다시 요청하세요."
            ),
        }

    # The parameter must EXIST in the group and be MODIFIABLE, both read from
    # describe_db_cluster_parameters. modify_db_cluster_parameter_group rejects a
    # pinned parameter and a name the engine family does not have, and BOTH of
    # those answers were previously bought with the DBA's single-use approval.
    found = _find_parameter(rds.describe_db_cluster_parameters,
                            "DBClusterParameterGroupName", pg_name, parameter_name)
    if found is _LOOKUP_FAILED:
        return {
            "status": "lookup_failed",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "reason": "클러스터 파라미터 그룹의 파라미터 목록을 조회할 수 없어 변경하지 "
                      "않았습니다 (자세한 원인은 서버 로그를 확인하세요).",
        }
    if found is None:
        return {
            "status": "unknown_parameter",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "parameter": parameter_name,
            "reason": f"클러스터 파라미터 그룹 '{pg_name}'에 {parameter_name!r} 파라미터가 "
                      "없습니다. 엔진/버전에 맞는 이름인지 확인하세요.",
        }

    # From here on the API's spelling IS the parameter's name: it goes into the
    # responses, into the approval payload and into the write, so the name the DBA
    # reads back on the card is the name that was actually sent to AWS. Same as
    # the instance tool. Without this the case-insensitive lookup accepted
    # " max_connections " / MAX_CONNECTIONS and then sent that raw string to
    # modify_db_cluster_parameter_group AFTER the guard had consumed the approval.
    parameter_name = str(found.get("ParameterName") or "").strip() or parameter_name
    # ParameterValue is ABSENT for a parameter left at the engine default, which
    # is not the same as an empty string (MEASURED: 279 of the 448 parameters in
    # pgtsd-demo-cpg carry no ParameterValue at all).
    current_value = found.get("ParameterValue")

    # Only an EXPLICIT False refuses. A response that does not carry the field has
    # not told us the parameter is fixed, and reporting it as fixed would be a
    # negative the data cannot support. MEASURED: describe_db_cluster_parameters
    # carries IsModifiable on every one of the 448 / 416 / 424 parameters in
    # pgtsd-demo-cpg, default.aurora-postgresql15 and default.aurora-mysql8.0.
    if found.get("IsModifiable") is False:
        return {
            "status": "not_modifiable",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "parameter": parameter_name,
            "current_value": current_value,
            "apply_type": found.get("ApplyType") or "",
            "reason": (
                f"'{parameter_name}' 파라미터는 이 클러스터 파라미터 그룹에서 수정할 수 "
                f"없습니다(describe_db_cluster_parameters의 IsModifiable=false). 엔진/버전 "
                f"단위로 AWS가 고정한 값이라 변경 요청 자체가 거부되므로, 승인을 소모하지 "
                f"않고 여기서 중단합니다. 현재 값은 "
                f"{(current_value if current_value is not None else '엔진 기본값')!r}입니다. "
                f"수정 가능한 다른 파라미터를 쓰거나, 엔진 버전 업그레이드가 필요한지 "
                f"확인하세요."
            ),
        }

    if not approved:
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "parameter": parameter_name,
            "value": value,
            "parameter_group": pg_name,
        }

    # TOCTOU, answered with what is ALREADY IN HAND, exactly as the instance tool
    # does it. `pg_name` is the group this cluster points at right now, read a few
    # lines up in THIS invocation, and `parameter_group` is the group the DBA's
    # approval is hash-bound to.
    #
    # Without this, the group was informational only: the projection bound
    # {parameter_name, value}, so a cluster re-pointed to a DIFFERENT cluster
    # parameter group between the approval and the execute had the write land on
    # the new group while the card named none. Same value, same parameter, wrong
    # group, and nothing in the record said so. Binding the group is what makes the
    # target part of what the DBA approved.
    #
    # An empty/omitted `parameter_group` lands here too, and the reason says
    # "different from", not "changed": the arg may simply have been left out, and
    # asserting drift would be a claim the data does not support. Either way the
    # approval SURVIVES, because this returns before verify_approval.
    if pg_name != parameter_group:
        return {
            "status": "state_changed",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "approved_parameter_group": parameter_group,
            "reason": (
                f"승인에 기록된 클러스터 파라미터 그룹({parameter_group!r})이 이 클러스터가 "
                f"현재 사용하는 그룹({pg_name!r})과 다릅니다. 승인 이후 그룹이 바뀌었을 수 "
                f"있습니다. 승인을 소모하지 않았으니, 현재 그룹으로 다시 승인 요청하세요."
            ),
        }

    # `parameter_name` is the API's spelling by now, which is what the
    # approval_required card above advertised too, and the projection folds both
    # anyway, so a card registered from the DBA's typed spelling still verifies.
    guard = verify_approval(
        approval_id,
        cluster_id,
        "modify_parameter",
        payload={"parameter_name": parameter_name, "value": value,
                 "parameter_group": parameter_group},
    )
    if not guard.get("ok"):
        return {
            "status": "approval_denied",
            "reason": guard.get("reason", "approval guard rejected the request"),
            "cluster_id": cluster_id,
            "parameter": parameter_name,
            "value": value,
        }

    try:
        rds.modify_db_cluster_parameter_group(
            DBClusterParameterGroupName=pg_name,
            Parameters=[{
                "ParameterName": parameter_name,
                "ParameterValue": value,
                "ApplyMethod": "pending-reboot",
            }],
        )
    except Exception:
        logger.warning(
            "modify_db_cluster_parameter_group failed for %s (group=%s, param=%s)",
            cluster_id, pg_name, parameter_name, exc_info=True,
        )
        # What is LEFT to fail here after the preflight: the value's allowed range,
        # and permissions (cross-account, the ManagedBy=dbops tag has to be on the
        # cluster parameter GROUP). Name those, mirroring the instance tool: this
        # reason is the DBA's only pointer, and the approval is already spent.
        return {
            "status": "modify_failed",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "parameter": parameter_name,
            "reason": "클러스터 파라미터 그룹 수정에 실패했습니다 (값 유효 범위·권한·클러스터 "
                      "파라미터 그룹 태그를 확인하세요).",
        }

    # 변경은 ApplyMethod=pending-reboot로 등록된다 — 파라미터 그룹에 값은
    # 들어가지만 실제 동작값(pg_settings)은 인스턴스 재시작 전까지 안 바뀐다.
    # 이 안내가 없으면 에이전트가 "변경 완료"라고만 답하고, DBA는 대시보드에
    # 즉시 반영을 기대하다 혼란스러워한다(승인 후 안 바뀐다는 실제 피드백).
    return {
        "status": "modified",
        "cluster_id": cluster_id,
        "parameter_group": pg_name,
        "parameter": parameter_name,
        "value": value,
        "apply_method": "pending-reboot",
        "applied": False,
        "note": (
            f"파라미터 그룹 '{pg_name}'에 {parameter_name}={value}로 등록했습니다. "
            "ApplyMethod=pending-reboot이라 **실제 적용에는 인스턴스 재시작이 필요**합니다 — "
            "재시작 전까지 pg_settings(동작값)는 바뀌지 않습니다. "
            "또한 대시보드의 PostgreSQL Configuration은 5분 주기 수집 캐시라, "
            "재시작 후에도 최대 5분 뒤에 반영됩니다. 재시작 시점은 유지보수 윈도우 또는 "
            "manage_maintenance로 계획하세요."
        ),
    }
