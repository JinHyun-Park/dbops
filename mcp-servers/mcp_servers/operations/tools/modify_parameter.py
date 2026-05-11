import boto3
from mcp_servers.shared.cache_client import CacheClient


def modify_parameter_impl(cache: CacheClient, cluster_id: str, parameter_name: str, value: str, approved: bool = False) -> dict:
    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "parameter": parameter_name, "value": value}

    rds = boto3.client("rds")

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
