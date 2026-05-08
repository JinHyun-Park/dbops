from mcp_servers.shared.cache_client import CacheClient


def detect_anomalies_impl(
    cache: CacheClient,
    cluster_id: str,
    hours: int = 4,
    threshold: float = 2.0,
) -> dict:
    sql = """
        WITH recent AS (
            SELECT metric_type, AVG(value) as current_avg
            FROM metric_snapshots
            WHERE cluster_id = :cluster_id AND ts > NOW() - MAKE_INTERVAL(hours => :hours)
            GROUP BY metric_type
        ),
        baseline AS (
            SELECT metric_type, AVG(value) as baseline_avg, STDDEV(value) as baseline_stddev
            FROM metric_snapshots
            WHERE cluster_id = :cluster_id
              AND ts > NOW() - INTERVAL '7 days'
              AND ts <= NOW() - MAKE_INTERVAL(hours => :hours)
            GROUP BY metric_type
        )
        SELECT r.metric_type, r.current_avg, b.baseline_avg, b.baseline_stddev,
               CASE WHEN b.baseline_stddev > 0 THEN (r.current_avg - b.baseline_avg) / b.baseline_stddev ELSE 0 END as z_score
        FROM recent r JOIN baseline b ON r.metric_type = b.metric_type
        ORDER BY ABS(CASE WHEN b.baseline_stddev > 0 THEN (r.current_avg - b.baseline_avg) / b.baseline_stddev ELSE 0 END) DESC
    """
    params = {"cluster_id": cluster_id, "hours": hours}
    result = cache.execute(sql, params)
    anomalies = [r for r in result.rows if abs(float(r.get("z_score", 0))) >= threshold]
    return {
        "cluster_id": cluster_id,
        "hours": hours,
        "threshold": threshold,
        "anomalies": anomalies,
        "total_checked": result.row_count,
    }
