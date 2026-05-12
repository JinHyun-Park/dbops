"""MySQL counterpart to pg_table_stats.py.

MySQL doesn't expose seq_scan / idx_scan / live_tup / dead_tup in the same
form as Postgres. We map:
  - n_live_tup  → information_schema.tables.TABLE_ROWS (estimate)
  - n_dead_tup  → information_schema.tables.DATA_FREE/avg_row_length (estimate;
                  this is the "fragmented" tuple equivalent for InnoDB)
  - total_bytes → DATA_LENGTH + INDEX_LENGTH
  - table_bytes → DATA_LENGTH
  - index_bytes → INDEX_LENGTH
  - seq_scan    → performance_schema.table_io_waits_summary_by_table.COUNT_READ
                  (table-level reads, not strictly sequential)
  - idx_scan    → performance_schema.table_io_waits_summary_by_index_usage
                  per-index COUNT_FETCH summed
  - last_vacuum / last_analyze → NULL (MySQL doesn't expose this)
"""

TABLE_STATS_SQL = """
SELECT
  t.TABLE_SCHEMA AS schema_name,
  t.TABLE_NAME   AS table_name,
  COALESCE(t.TABLE_ROWS, 0) AS n_live_tup,
  COALESCE(FLOOR(t.DATA_FREE / NULLIF(t.AVG_ROW_LENGTH, 0)), 0) AS n_dead_tup,
  COALESCE(SUM(CASE WHEN ios.INDEX_NAME IS NULL THEN ios.COUNT_FETCH ELSE 0 END), 0) AS seq_scan,
  COALESCE(SUM(CASE WHEN ios.INDEX_NAME IS NOT NULL THEN ios.COUNT_FETCH ELSE 0 END), 0) AS idx_scan,
  COALESCE(SUM(CASE WHEN ios.INDEX_NAME IS NULL THEN ios.COUNT_READ ELSE 0 END), 0) AS seq_tup_read,
  COALESCE(SUM(CASE WHEN ios.INDEX_NAME IS NOT NULL THEN ios.COUNT_READ ELSE 0 END), 0) AS idx_tup_fetch,
  COALESCE(t.DATA_LENGTH + t.INDEX_LENGTH, 0) AS total_bytes,
  COALESCE(t.DATA_LENGTH, 0) AS table_bytes,
  COALESCE(t.INDEX_LENGTH, 0) AS index_bytes
FROM information_schema.tables t
LEFT JOIN performance_schema.table_io_waits_summary_by_index_usage ios
  ON ios.OBJECT_SCHEMA = t.TABLE_SCHEMA AND ios.OBJECT_NAME = t.TABLE_NAME
WHERE t.TABLE_SCHEMA NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
  AND t.TABLE_TYPE = 'BASE TABLE'
GROUP BY t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_ROWS, t.DATA_FREE, t.AVG_ROW_LENGTH, t.DATA_LENGTH, t.INDEX_LENGTH
ORDER BY total_bytes DESC
LIMIT 100
"""


INSERT_SQL = (
    "INSERT INTO table_stats "
    "(cluster_id, snapshot_time, schema_name, table_name, "
    " n_live_tup, n_dead_tup, seq_scan, idx_scan, "
    " seq_tup_read, idx_tup_fetch, last_vacuum, last_analyze, "
    " total_bytes, table_bytes, index_bytes) "
    "VALUES (:cluster_id, NOW(), :schema_name, :table_name, "
    " :n_live_tup, :n_dead_tup, :seq_scan, :idx_scan, "
    " :seq_tup_read, :idx_tup_fetch, NULL, NULL, "
    " :total_bytes, :table_bytes, :index_bytes)"
)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def collect_mysql_table_stats(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {TABLE_STATS_SQL}",
        includeResultMetadata=True,
    )

    inserted = 0
    for rec in resp.get("records", []):
        params = {
            "cluster_id": cluster_id,
            "schema_name": _str(rec[0]),
            "table_name": _str(rec[1]),
            "n_live_tup": _long(rec[2]),
            "n_dead_tup": _long(rec[3]),
            "seq_scan": _long(rec[4]),
            "idx_scan": _long(rec[5]),
            "seq_tup_read": _long(rec[6]),
            "idx_tup_fetch": _long(rec[7]),
            "total_bytes": _long(rec[8]),
            "table_bytes": _long(rec[9]),
            "index_bytes": _long(rec[10]),
        }
        cache_execute(INSERT_SQL, params)
        inserted += 1

    return {"cluster_id": cluster_id, "tables_collected": inserted}
