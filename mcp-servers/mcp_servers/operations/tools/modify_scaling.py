import boto3

from mcp_servers.shared.cache_client import CacheClient


def modify_scaling_impl(cache: CacheClient, cluster_id: str, min_capacity: float = None, max_capacity: float = None, approved: bool = False) -> dict:
    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "min_capacity": min_capacity, "max_capacity": max_capacity}

    rds = boto3.client("rds")
    params = {"DBClusterIdentifier": cluster_id}
    sc = {}
    if min_capacity is not None:
        sc["MinCapacity"] = min_capacity
    if max_capacity is not None:
        sc["MaxCapacity"] = max_capacity
    if sc:
        params["ServerlessV2ScalingConfiguration"] = sc

    rds.modify_db_cluster(**params)
    return {"status": "modified", "cluster_id": cluster_id, "scaling": sc}
