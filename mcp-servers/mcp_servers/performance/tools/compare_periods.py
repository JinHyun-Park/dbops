from mcp_servers.shared.cache_client import CacheClient


def compare_periods_impl(
    cache: CacheClient,
    cluster_id: str,
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    metric_type: str = "aas",
) -> dict:
    def get_avg(start: str, end: str) -> dict:
        sql = (
            "SELECT AVG(value) as avg_value, MAX(value) as max_value, "
            "MIN(value) as min_value, COUNT(*) as sample_count "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cluster_id AND metric_type = :metric_type "
            "AND ts >= :start_time AND ts < :end_time"
        )
        params = {
            "cluster_id": cluster_id,
            "metric_type": metric_type,
            "start_time": start,
            "end_time": end,
        }
        result = cache.execute(sql, params)
        return result.rows[0] if result.rows else {}

    period_a = get_avg(period_a_start, period_a_end)
    period_b = get_avg(period_b_start, period_b_end)
    return {
        "cluster_id": cluster_id,
        "metric_type": metric_type,
        "period_a": {"start": period_a_start, "end": period_a_end, **period_a},
        "period_b": {"start": period_b_start, "end": period_b_end, **period_b},
    }
