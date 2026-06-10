from mcp_servers.shared.cache_client import CacheClient


def get_recent_events_impl(
    cache: CacheClient,
    cluster_id: str,
    hours: int = 24,
    event_type: str = None,
) -> dict:
    conditions = [
        "cluster_id = :cluster_id",
        "event_time > NOW() - (:hours || ' hours')::interval",
    ]
    params = {"cluster_id": cluster_id, "hours": hours}
    if event_type:
        conditions.append("event_type = :event_type")
        params["event_type"] = event_type
    sql = f"SELECT * FROM event_log WHERE {' AND '.join(conditions)} ORDER BY event_time DESC LIMIT 100"
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "events": result.rows,
        "count": result.row_count,
    }
