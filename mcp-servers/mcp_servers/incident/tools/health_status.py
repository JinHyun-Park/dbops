import json

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.metric_filters import CLUSTER_LEVEL_ONLY

# Engines that are NOT relational Aurora clusters — for these we surface
# engine + parsed resource_details so the agent has billing mode, capacity,
# GSI/LSI counts, instance topology, etc. without a separate lookup.
_NON_RELATIONAL_ENGINES = {"dynamodb", "docdb", "documentdb"}


def get_health_status_impl(cache: CacheClient, cluster_id: str) -> dict:
    meta_sql = "SELECT * FROM cluster_meta WHERE cluster_id = :cluster_id"
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})

    metrics_sql = f"""
        SELECT metric_type, AVG(value) as avg_val, MAX(value) as max_val
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id AND ts > NOW() - INTERVAL '10 minutes'
          {CLUSTER_LEVEL_ONLY}
        GROUP BY metric_type
    """
    metrics = cache.execute(metrics_sql, {"cluster_id": cluster_id})

    cluster = meta.rows[0] if meta.rows else {}
    status = cluster.get("status", "unknown")
    health = (
        "healthy"
        if status == "available"
        else "warning"
        if status in ("modifying", "backing-up")
        else "critical"
    )

    result: dict = {
        "cluster_id": cluster_id,
        "health": health,
        "cluster": cluster,
        "current_metrics": metrics.rows,
    }

    engine = cluster.get("engine", "")
    if engine in _NON_RELATIONAL_ENGINES:
        result["engine"] = engine
        raw_details = cluster.get("resource_details")
        if raw_details is not None:
            if isinstance(raw_details, str):
                try:
                    result["resource_details"] = json.loads(raw_details)
                except (json.JSONDecodeError, ValueError):
                    result["resource_details"] = raw_details
            else:
                result["resource_details"] = raw_details
        else:
            result["resource_details"] = None

    return result
