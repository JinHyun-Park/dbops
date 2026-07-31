from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster
from mcp_servers.shared.managed_tag_preflight import aurora_cluster_tag_warning


def manage_maintenance_impl(
    cache: CacheClient,
    cluster_id: str,
    action: str = "describe",
    window: str = None,
    approved: bool = False,
    approval_id: str = "",
) -> dict:
    rds = rds_client_for_cluster(cluster_id)

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
            card = {"status": "approval_required", "action": "modify_maintenance", "window": window}
            # NOTE the describe_db_clusters above is inside the `describe` branch,
            # which returns, so there is no cluster ARN in hand on THIS path. The
            # helper resolves it. Cross-account only, WARNING never a refusal.
            tag_warning = aurora_cluster_tag_warning(
                rds, cluster_id, action="rds:ModifyDBCluster")
            if tag_warning:
                card["warning"] = tag_warning
            return card

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
