from mcp_servers.shared.cache_client import CacheClient

SORT_COLUMNS = {
    "total_time": "total_time_ms DESC",
    "calls": "calls DESC",
    "mean_time": "mean_time_ms DESC",
    "rows": "rows_returned DESC",
}


def get_top_queries_impl(
    cache: CacheClient,
    cluster_id: str,
    sort_by: str = "total_time",
    limit: int = 10,
    start_time: str = None,
    end_time: str = None,
) -> dict:
    order = SORT_COLUMNS.get(sort_by, "total_time_ms DESC")
    sql, params = cache._build_query(
        table="query_stats",
        cluster_id=cluster_id,
        time_column="snapshot_time" if start_time else None,
        start_time=start_time,
        end_time=end_time,
        order_by=order,
        limit=limit,
    )
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "sort_by": sort_by,
        "row_count": result.row_count,
        "queries": result.rows,
    }
