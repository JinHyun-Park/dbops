from mcp_servers.shared.cache_client import CacheClient


def correlate_signals_impl(
    cache: CacheClient,
    cluster_id: str,
    start_time: str,
    end_time: str,
) -> dict:
    metrics_sql = """
        SELECT ts as event_time, 'metric' as signal_type, metric_type as detail, value::text as value
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id AND ts >= :start_time::timestamptz AND ts < :end_time::timestamptz
        AND metric_type IN ('aas', 'cpu', 'db_connections')
    """
    events_sql = """
        SELECT event_time, 'event' as signal_type, event_type as detail, message as value
        FROM event_log
        WHERE cluster_id = :cluster_id AND event_time >= :start_time::timestamptz AND event_time < :end_time::timestamptz
    """
    combined_sql = f"({metrics_sql}) UNION ALL ({events_sql}) ORDER BY event_time"
    params = {
        "cluster_id": cluster_id,
        "start_time": start_time,
        "end_time": end_time,
    }
    result = cache.execute(combined_sql, params)
    return {
        "cluster_id": cluster_id,
        "start_time": start_time,
        "end_time": end_time,
        "timeline": result.rows,
        "count": result.row_count,
    }
