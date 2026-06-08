from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster


def modify_scaling_impl(
    cache: CacheClient,
    cluster_id: str,
    min_capacity: float = None,
    max_capacity: float = None,
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    if not approved:
        return {"status": "approval_required", "cluster_id": cluster_id, "min_capacity": min_capacity, "max_capacity": max_capacity}

    guard = verify_approval(
        approval_id,
        cluster_id,
        "modify_scaling",
        payload={"min_capacity": min_capacity, "max_capacity": max_capacity},
    )
    if not guard.get("ok"):
        return {
            "status": "approval_denied",
            "reason": guard.get("reason", "approval guard rejected the request"),
            "cluster_id": cluster_id,
        }

    rds = rds_client_for_cluster(cluster_id)
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
