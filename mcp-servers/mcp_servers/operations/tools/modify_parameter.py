"""modify_parameter: approval-gated Aurora CLUSTER parameter-group change.

Aurora-relational only. The handler positive-gates this tool on the
`cluster_parameter` capability (FAIL-CLOSED), so rds_instance (which has
INSTANCE parameter groups), DocumentDB, DynamoDB and ElastiCache refuse with
unsupported_engine instead of failing inside `rds.describe_db_clusters`.

Failures return a STATIC Korean reason and log the detail with the module
logger: raw exception text must never reach a tool response.
"""

import logging

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster

logger = logging.getLogger(__name__)


def modify_parameter_impl(
    cache: CacheClient,
    cluster_id: str,
    parameter_name: str,
    value: str,
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    # Every static refusal happens BEFORE verify_approval (same ordering as
    # set_docdb_profiler). The approval is SINGLE-USE: consuming it and then
    # refusing on a precondition that was already true at approval time burnt the
    # approval, and the retry died with "already consumed".
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

    # Refuse to mutate the AWS-managed `default.*` parameter group — modifying it
    # would require creating a custom group first, which is its own workflow.
    if pg_name.startswith("default."):
        return {
            "status": "default_group_refused",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "reason": "Cluster uses an AWS-default parameter group; create a custom group first.",
        }

    if not approved:
        return {
            "status": "approval_required",
            "cluster_id": cluster_id,
            "parameter": parameter_name,
            "value": value,
            "parameter_group": pg_name,
        }

    # parameter_group is informational here: the approval projection binds
    # {parameter_name, value} only, so echoing the group into action_details
    # cannot break the hash.
    guard = verify_approval(
        approval_id,
        cluster_id,
        "modify_parameter",
        payload={"parameter_name": parameter_name, "value": value},
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
        return {
            "status": "modify_failed",
            "cluster_id": cluster_id,
            "parameter_group": pg_name,
            "reason": "파라미터 그룹 수정에 실패했습니다 (자세한 원인은 서버 로그를 확인하세요).",
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
