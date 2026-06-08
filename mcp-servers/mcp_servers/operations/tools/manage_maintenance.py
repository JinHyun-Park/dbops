import boto3

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient


def manage_maintenance_impl(
    cache: CacheClient,
    cluster_id: str,
    action: str = "describe",
    window: str = None,
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    rds = boto3.client("rds")

    if action == "describe":
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = resp["DBClusters"][0]
        return {
            "cluster_id": cluster_id,
            "maintenance_window": cluster.get("PreferredMaintenanceWindow", ""),
            "pending_maintenance": cluster.get("PendingModifiedValues", {}),
        }

    if action == "modify" and window:
        if not approved:
            return {"status": "approval_required", "action": "modify_maintenance", "window": window}

        guard = verify_approval(
            approval_id, cluster_id, "manage_maintenance", payload={"window": window}
        )
        if not guard.get("ok"):
            return {
                "status": "approval_denied",
                "reason": guard.get("reason", "approval guard rejected the request"),
                "cluster_id": cluster_id,
                "window": window,
            }

        rds.modify_db_cluster(DBClusterIdentifier=cluster_id, PreferredMaintenanceWindow=window)
        return {"status": "modified", "cluster_id": cluster_id, "new_window": window}

    return {"error": f"Unknown action: {action}"}
