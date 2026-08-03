from mcp_servers.shared.cache_client import CacheClient

# Newest-first, so a truncated read keeps the recent end. Echoed to the caller
# rather than applied silently.
_LIMIT = 100


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
    sql = (
        f"SELECT * FROM event_log WHERE {' AND '.join(conditions)} "
        f"ORDER BY event_time DESC LIMIT {_LIMIT}"
    )
    result = cache.execute(sql, params)
    # Echo the window and the limit. `count: 0` alone reads as "no recent events",
    # and the agent renders it that way, for a cluster whose last anomaly was 26
    # hours ago: the same probe that saw 0 here saw 43-55 events from
    # get_incident_summary's 30-day window on the same clusters. The limit is echoed
    # for the opposite reason: 100 events with no note reads as all of them.
    return {
        "cluster_id": cluster_id,
        "events": result.rows,
        "count": result.row_count,
        "window_hours": hours,
        "event_type": event_type,
        "limit": _LIMIT,
        "truncated": result.row_count >= _LIMIT,
    }
