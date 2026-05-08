from mcp_servers.shared.cache_client import CacheClient

METRIC_LIMITS = {"storage_gb": 128000, "connections": 5000, "aas": 64}


def forecast_capacity_impl(
    cache: CacheClient,
    cluster_id: str,
    metric: str = "storage_gb",
    days_lookback: int = 30,
) -> dict:
    sql = """
        SELECT
            REGR_SLOPE(value, EXTRACT(EPOCH FROM ts) / 86400) as slope_per_day,
            MAX(value) as current_value
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id AND metric_type = :metric
          AND ts > NOW() - MAKE_INTERVAL(days => :days_lookback)
    """
    params = {"cluster_id": cluster_id, "metric": metric, "days_lookback": days_lookback}
    result = cache.execute(sql, params)
    row = result.rows[0] if result.rows else {}
    slope = float(row.get("slope_per_day", 0))
    current = float(row.get("current_value", 0))
    limit = METRIC_LIMITS.get(metric, 1000)
    days_until = int((limit - current) / slope) if slope > 0 else -1

    return {
        "cluster_id": cluster_id,
        "metric": metric,
        "current_value": current,
        "limit": limit,
        "slope_per_day": round(slope, 4),
        "days_until_limit": days_until,
        "forecast": "growing" if slope > 0 else "stable" if slope == 0 else "shrinking",
    }
