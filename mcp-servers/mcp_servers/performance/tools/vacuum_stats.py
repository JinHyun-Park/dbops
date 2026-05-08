from mcp_servers.shared.cache_client import CacheClient


def get_vacuum_stats_impl(cache: CacheClient, cluster_id: str) -> dict:
    sql = """
        SELECT schemaname, relname as table_name,
               n_dead_tup as dead_tuples, n_live_tup as live_tuples,
               CASE WHEN n_live_tup > 0 THEN ROUND(n_dead_tup::numeric / n_live_tup * 100, 2) ELSE 0 END as bloat_pct,
               last_vacuum, last_autovacuum, last_analyze, last_autoanalyze,
               vacuum_count, autovacuum_count
        FROM pg_stat_user_tables
        ORDER BY n_dead_tup DESC
        LIMIT 20
    """
    result = cache.execute(sql, {"cluster_id": cluster_id})

    warnings = []
    for row in result.rows:
        bloat = float(row.get("bloat_pct", 0))
        if bloat > 20:
            warnings.append(f"Table {row.get('table_name')} has {bloat}% bloat ({row.get('dead_tuples')} dead tuples)")

    return {"cluster_id": cluster_id, "tables": result.rows, "warnings": warnings, "table_count": result.row_count}
