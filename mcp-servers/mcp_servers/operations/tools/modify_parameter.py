from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster


def modify_parameter_impl(
    cache: CacheClient,
    cluster_id: str,
    parameter_name: str,
    value: str,
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "parameter": parameter_name, "value": value}

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

    rds = rds_client_for_cluster(cluster_id)

    # Look up the cluster's actual parameter group rather than guessing a name.
    try:
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = (resp.get("DBClusters") or [{}])[0]
    except Exception as e:
        return {"status": "lookup_failed", "cluster_id": cluster_id, "error": str(e)}

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

    try:
        rds.modify_db_cluster_parameter_group(
            DBClusterParameterGroupName=pg_name,
            Parameters=[{
                "ParameterName": parameter_name,
                "ParameterValue": value,
                "ApplyMethod": "pending-reboot",
            }],
        )
    except Exception as e:
        return {"status": "modify_failed", "cluster_id": cluster_id, "parameter_group": pg_name, "error": str(e)}

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
