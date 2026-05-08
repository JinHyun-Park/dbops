import boto3
from mcp_servers.shared.cache_client import CacheClient


def modify_parameter_impl(cache: CacheClient, cluster_id: str, parameter_name: str, value: str, approved: bool = False) -> dict:
    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "parameter": parameter_name, "value": value}

    rds = boto3.client("rds")
    pg_name = f"dbops-{cluster_id}-params"

    rds.modify_db_cluster_parameter_group(
        DBClusterParameterGroupName=pg_name,
        Parameters=[{
            "ParameterName": parameter_name,
            "ParameterValue": value,
            "ApplyMethod": "pending-reboot",
        }],
    )
    return {"status": "modified", "cluster_id": cluster_id, "parameter": parameter_name, "value": value}
