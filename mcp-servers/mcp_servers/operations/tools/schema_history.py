from mcp_servers.shared.cache_client import CacheClient


def get_schema_history_impl(cache: CacheClient, cluster_id: str, days: int = 30) -> dict:
    sql = """
        SELECT snapshot_time, schema_name, diff_from_previous_json as changes
        FROM schema_snapshots
        WHERE cluster_id = :cluster_id AND snapshot_time > NOW() - (:days || ' days')::interval
          AND diff_from_previous_json IS NOT NULL AND diff_from_previous_json != '{}'
        ORDER BY snapshot_time DESC
    """
    params = {"cluster_id": cluster_id, "days": days}
    result = cache.execute(sql, params)
    return {"cluster_id": cluster_id, "period_days": days, "changes": result.rows, "count": result.row_count}
