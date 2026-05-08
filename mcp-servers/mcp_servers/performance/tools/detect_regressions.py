from mcp_servers.shared.cache_client import CacheClient


def detect_regressions_impl(
    cache: CacheClient,
    cluster_id: str,
    change_point: str,
    hours_before: int = 24,
    hours_after: int = 24,
    min_change_pct: float = 50.0,
) -> dict:
    sql = """
        WITH before_period AS (
            SELECT query_hash, query_text, AVG(mean_time_ms) as before_mean_ms
            FROM query_stats
            WHERE cluster_id = :cluster_id
              AND snapshot_time >= :change_point::timestamptz - MAKE_INTERVAL(hours => :hours_before)
              AND snapshot_time < :change_point::timestamptz
            GROUP BY query_hash, query_text
        ),
        after_period AS (
            SELECT query_hash, AVG(mean_time_ms) as after_mean_ms
            FROM query_stats
            WHERE cluster_id = :cluster_id
              AND snapshot_time >= :change_point::timestamptz
              AND snapshot_time < :change_point::timestamptz + MAKE_INTERVAL(hours => :hours_after)
            GROUP BY query_hash
        )
        SELECT b.query_hash, b.query_text, b.before_mean_ms, a.after_mean_ms,
               ROUND(((a.after_mean_ms - b.before_mean_ms) / NULLIF(b.before_mean_ms, 0)) * 100, 1) as change_pct
        FROM before_period b JOIN after_period a ON b.query_hash = a.query_hash
        WHERE a.after_mean_ms > b.before_mean_ms * (1 + :min_change_pct / 100.0)
        ORDER BY change_pct DESC LIMIT 20
    """
    params = {
        "cluster_id": cluster_id,
        "change_point": change_point,
        "hours_before": hours_before,
        "hours_after": hours_after,
        "min_change_pct": min_change_pct,
    }
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "change_point": change_point,
        "regressions": result.rows,
        "count": result.row_count,
    }
