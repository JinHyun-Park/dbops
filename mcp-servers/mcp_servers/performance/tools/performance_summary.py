from mcp_servers.shared.cache_client import CacheClient


def get_performance_summary_impl(
    cache: CacheClient,
    cluster_id: str,
    hours: int = 24,
) -> dict:
    sql = """
        SELECT
            (SELECT AVG(value) FROM metric_snapshots WHERE cluster_id = :cluster_id AND metric_type = 'aas' AND ts > NOW() - MAKE_INTERVAL(hours => :hours)) as avg_aas,
            (SELECT MAX(value) FROM metric_snapshots WHERE cluster_id = :cluster_id AND metric_type = 'aas' AND ts > NOW() - MAKE_INTERVAL(hours => :hours)) as max_aas,
            (SELECT COUNT(*) FROM slow_queries WHERE cluster_id = :cluster_id AND ts > NOW() - MAKE_INTERVAL(hours => :hours)) as slow_count,
            (SELECT MAX(value) FROM metric_snapshots WHERE cluster_id = :cluster_id AND metric_type = 'connections' AND ts > NOW() - MAKE_INTERVAL(hours => :hours)) as peak_connections
    """
    params = {"cluster_id": cluster_id, "hours": hours}
    result = cache.execute(sql, params)
    kpis = result.rows[0] if result.rows else {}
    return {
        "cluster_id": cluster_id,
        "period_hours": hours,
        "kpis": kpis,
    }
