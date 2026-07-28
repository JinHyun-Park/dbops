from mcp_servers.shared.cache_client import CacheClient, is_mysql_engine

# PG: dead tuples as a % of live tuples. Long-standing threshold for this tool.
_PG_BLOAT_WARN_PCT = 20.0
# MySQL/InnoDB: DATA_FREE is reclaimable space INSIDE the tablespace, and some is
# normal (the free-list holds pages for reuse). A rebuild (OPTIMIZE TABLE) copies
# the whole table, so the bar has to be higher than PG's before recommending it.
# Measured on the live Aurora MySQL demo cluster: 11.3% and 9.3% on the two
# tables, i.e. ordinary free-list churn, and neither should raise a warning.
_MYSQL_FRAGMENTATION_WARN_PCT = 25.0

# InnoDB has no dead tuples and no VACUUM, so reporting the MySQL number under
# PG names ("dead_tuples", "bloat_pct", "last_vacuum") tells a MySQL DBA to run
# maintenance that does not exist. The number itself is real, so it is RELABELLED
# rather than withheld.
_MYSQL_SOURCE_NOTE = (
    "InnoDB에는 dead tuple도 VACUUM도 없습니다. 이 값은 "
    "information_schema.tables.DATA_FREE를 AVG_ROW_LENGTH로 나눈 추정치로, "
    "테이블스페이스 안에서 재사용 가능한 여유 공간을 행 수로 환산한 것입니다"
    "(바이트가 아닙니다). 일정 수준의 여유 공간은 free-list 재사용분이라 정상이며, "
    "줄이려면 OPTIMIZE TABLE로 테이블을 재구축해야 합니다. MySQL 수집기는 "
    "last_vacuum / last_analyze를 채우지 않으므로 이 응답에서는 아예 제외합니다."
)


def get_vacuum_stats_impl(cache: CacheClient, cluster_id: str) -> dict:
    # Read the pre-collected `table_stats` cache (populated by the ETL collector
    # from each cluster's pg_stat_user_tables), scoped to THIS cluster and the
    # latest snapshot per table. The previous version queried
    # `pg_stat_user_tables` on the cache DB itself with no cluster filter, so it
    # reported the DBOps cache's own internal tables instead of the target
    # cluster's. This matches the dashboard /vacuum-stats source.
    #
    # The MySQL collectors write the SAME table with n_dead_tup carrying a
    # DATA_FREE-derived estimate, so the query is shared and only the LABELS on
    # the way out differ (see _mysql_payload).
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

    # Aurora PG and Aurora MySQL are the SAME capability family (relational), so
    # this cannot be gated by a family flag: it is resolved from the engine
    # string. is_mysql_engine also matches standalone RDS MySQL on purpose: its
    # collector fills n_dead_tup from the identical DATA_FREE expression, so the
    # relabelling is correct for both, and SQL Server does not match.
    if is_mysql_engine(cache.engine_of(cluster_id)):
        return _mysql_payload(cluster_id, result)

    warnings = []
    for row in result.rows:
        bloat = float(row.get("bloat_pct", 0))
        if bloat > _PG_BLOAT_WARN_PCT:
            warnings.append(f"Table {row.get('table_name')} has {bloat}% bloat ({row.get('dead_tuples')} dead tuples)")

    return {"cluster_id": cluster_id, "engine": "postgresql", "tables": result.rows,
            "warnings": warnings, "table_count": result.row_count}


def _mysql_payload(cluster_id: str, result) -> dict:
    """Same measured numbers, MySQL names. `last_vacuum`/`last_analyze` are
    dropped instead of emitted as null: the MySQL collectors insert literal NULL
    there, and a null "last_vacuum" reads as "VACUUM has never run" when the
    truth is that the concept does not exist."""
    tables = []
    warnings = []
    for row in result.rows:
        pct = float(row.get("bloat_pct") or 0)
        table = row.get("table_name")
        tables.append({
            "schemaname": row.get("schemaname"),
            "table_name": table,
            # Rows-equivalent of DATA_FREE, NOT a byte count and NOT dead tuples.
            "free_rows_est": row.get("dead_tuples"),
            "live_tuples": row.get("live_tuples"),
            "fragmentation_pct": pct,
        })
        if pct > _MYSQL_FRAGMENTATION_WARN_PCT:
            warnings.append(
                f"Table {table} has {pct}% reclaimable free space "
                f"(~{row.get('dead_tuples')} rows worth of DATA_FREE); "
                f"consider OPTIMIZE TABLE during a maintenance window."
            )
    return {
        "cluster_id": cluster_id,
        "engine": "mysql",
        "tables": tables,
        "warnings": warnings,
        "table_count": result.row_count,
        "metric": "fragmentation_pct (DATA_FREE / live rows)",
        "threshold_pct": _MYSQL_FRAGMENTATION_WARN_PCT,
        "source": _MYSQL_SOURCE_NOTE,
    }
