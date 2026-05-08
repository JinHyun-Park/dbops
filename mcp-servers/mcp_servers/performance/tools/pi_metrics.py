from mcp_servers.shared.cache_client import CacheClient


def get_pi_metrics_impl(
    cache: CacheClient,
    cluster_id: str,
    metric_type: str = "aas",
    start_time: str = None,
    end_time: str = None,
) -> dict:
    sql, params = cache._build_query(
        table="metric_snapshots",
        cluster_id=cluster_id,
        time_column="ts",
        start_time=start_time,
        end_time=end_time,
        extra_where="metric_type = :metric_type" if metric_type else None,
        order_by="ts ASC",
    )
    if metric_type:
        params["metric_type"] = metric_type
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "metric_type": metric_type,
        "data_points": result.rows,
        "count": result.row_count,
    }
