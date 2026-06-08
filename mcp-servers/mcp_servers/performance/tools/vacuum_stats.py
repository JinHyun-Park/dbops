from mcp_servers.shared.cache_client import CacheClient


def get_vacuum_stats_impl(cache: CacheClient, cluster_id: str) -> dict:
    # Read the pre-collected `table_stats` cache (populated by the ETL collector
    # from each cluster's pg_stat_user_tables), scoped to THIS cluster and the
    # latest snapshot per table. The previous version queried
    # `pg_stat_user_tables` on the cache DB itself with no cluster filter, so it
    # reported the DBOps cache's own internal tables instead of the target
    # cluster's. This matches the dashboard /vacuum-stats source.
    sql = """
        WITH latest AS (
          SELECT DISTINCT ON (schema_name, table_name)
                 schema_name, table_name, n_live_tup, n_dead_tup,
                 last_vacuum, last_analyze
          FROM table_stats
          WHERE cluster_id = :cluster_id
            AND snapshot_time > NOW() - INTERVAL '24 hours'
          ORDER BY schema_name, table_name, snapshot_time DESC
        )
        SELECT schema_name AS schemaname, table_name,
               n_dead_tup AS dead_tuples, n_live_tup AS live_tuples,
               CASE WHEN n_live_tup > 0
                    THEN ROUND(n_dead_tup::numeric / n_live_tup * 100, 2)
                    ELSE 0 END AS bloat_pct,
               last_vacuum, last_analyze
        FROM latest
        ORDER BY n_dead_tup DESC NULLS LAST
        LIMIT 20
    """
    result = cache.execute(sql, {"cluster_id": cluster_id})

    warnings = []
    for row in result.rows:
        bloat = float(row.get("bloat_pct", 0))
        if bloat > 20:
            warnings.append(f"Table {row.get('table_name')} has {bloat}% bloat ({row.get('dead_tuples')} dead tuples)")

    return {"cluster_id": cluster_id, "tables": result.rows, "warnings": warnings, "table_count": result.row_count}
