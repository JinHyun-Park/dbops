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

    return {
        "status": "modified",
        "cluster_id": cluster_id,
        "parameter_group": pg_name,
        "parameter": parameter_name,
        "value": value,
    }
