from mcp_servers.shared.cache_client import CacheClient


def get_slow_queries_impl(
    cache: CacheClient,
    cluster_id: str,
    threshold_ms: float = 1000.0,
    limit: int = 20,
    start_time: str = None,
    end_time: str = None,
) -> dict:
    # Derived from query_stats (pg_stat_statements / perf-schema digests) by mean
    # execution time. The dedicated slow_queries table is defined in the schema
    # but no collector ever populates it, so querying it returned nothing. This
    # mirrors the dashboard's /slow-queries derivation (api/dashboard/handler.py
    # _slow_queries) so the agent and dashboard agree. NOTE: these are per-query
    # aggregates (mean time over the window), not individual slow executions /
    # an auto_explain / slow-query-log capture.
    sql, params = cache._build_query(
        table="query_stats",
        cluster_id=cluster_id,
        time_column="snapshot_time",
        start_time=start_time,
        end_time=end_time,
        extra_where="mean_time_ms >= :threshold_ms",
        order_by="mean_time_ms DESC",
        limit=limit,
    )
    params["threshold_ms"] = threshold_ms
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "threshold_ms": threshold_ms,
        "row_count": result.row_count,
        "queries": result.rows,
        "source": "query_stats.mean_time_ms (per-query aggregate, not a slow-query log)",
    }
