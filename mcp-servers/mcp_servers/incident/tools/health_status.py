from mcp_servers.shared.cache_client import CacheClient


def get_health_status_impl(cache: CacheClient, cluster_id: str) -> dict:
    meta_sql = "SELECT * FROM cluster_meta WHERE cluster_id = :cluster_id"
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})

    metrics_sql = """
        SELECT metric_type, AVG(value) as avg_val, MAX(value) as max_val
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id AND ts > NOW() - INTERVAL '10 minutes'
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

    return {
        "cluster_id": cluster_id,
        "health": health,
        "cluster": cluster,
        "current_metrics": metrics.rows,
    }
