from mcp_servers.shared.cache_client import CacheClient


def get_incident_summary_impl(
    cache: CacheClient,
    cluster_id: str,
    days: int = 30,
) -> dict:
    sql = """
        SELECT event_type, severity, COUNT(*) as count,
               MIN(event_time) as first_seen, MAX(event_time) as last_seen
        FROM event_log
        WHERE cluster_id = :cluster_id AND event_time > NOW() - MAKE_INTERVAL(days => :days)
        GROUP BY event_type, severity ORDER BY count DESC
    """
    params = {"cluster_id": cluster_id, "days": days}
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "period_days": days,
        "summary": result.rows,
        "total_events": sum(int(r.get("count", 0)) for r in result.rows),
    }
