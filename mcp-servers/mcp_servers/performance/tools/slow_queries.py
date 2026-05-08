from mcp_servers.shared.cache_client import CacheClient


def get_slow_queries_impl(
    cache: CacheClient,
    cluster_id: str,
    threshold_ms: float = 1000.0,
    limit: int = 20,
    start_time: str = None,
    end_time: str = None,
) -> dict:
    sql, params = cache._build_query(
        table="slow_queries",
        cluster_id=cluster_id,
        time_column="ts",
        start_time=start_time,
        end_time=end_time,
        extra_where="execution_time_ms >= :threshold_ms",
        order_by="execution_time_ms DESC",
        limit=limit,
    )
    params["threshold_ms"] = threshold_ms
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "threshold_ms": threshold_ms,
        "row_count": result.row_count,
        "queries": result.rows,
    }
