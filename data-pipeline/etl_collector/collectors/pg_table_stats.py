TABLE_STATS_SQL = """
SELECT
  s.schemaname,
  s.relname AS tablename,
  s.n_live_tup,
  s.n_dead_tup,
  s.seq_scan,
  s.idx_scan,
  s.seq_tup_read,
  s.idx_tup_fetch,
  COALESCE(s.last_vacuum, s.last_autovacuum) AS last_vacuum,
  COALESCE(s.last_analyze, s.last_autoanalyze) AS last_analyze,
  pg_total_relation_size(c.oid) AS total_bytes,
  pg_relation_size(c.oid) AS table_bytes,
  pg_indexes_size(c.oid) AS index_bytes
FROM pg_stat_user_tables s
JOIN pg_class c ON c.oid = s.relid
WHERE s.schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_total_relation_size(c.oid) DESC NULLS LAST
LIMIT 100
"""

# The LIMIT above is a deliberate size bound, and it means table_stats CANNOT
# answer "does this table exist": absence is "not in the 100 largest of that
# run", which is indistinguishable from a DROP, and top-100 membership churns on
# its own as tables grow. Anything needing table EXISTENCE (created / dropped)
# must read schema_snapshots, whose catalog read has no LIMIT anywhere. The
# dashboard schema-changes panel derived DDL from here and shipped two defects:
# an unreachable `dropped` branch and a `created` that fired on top-100
# entrants. See api/dashboard/handler.py::_schema_changes.


INSERT_SQL = (
    "INSERT INTO table_stats "
    "(cluster_id, snapshot_time, schema_name, table_name, "
    " n_live_tup, n_dead_tup, seq_scan, idx_scan, "
    " seq_tup_read, idx_tup_fetch, last_vacuum, last_analyze, "
    " total_bytes, table_bytes, index_bytes) "
    "VALUES (:cluster_id, NOW(), :schema_name, :table_name, "
    " :n_live_tup, :n_dead_tup, :seq_scan, :idx_scan, "
    " :seq_tup_read, :idx_tup_fetch, "
    " CASE WHEN :last_vacuum_str='' THEN NULL ELSE :last_vacuum_str::timestamptz END, "
    " CASE WHEN :last_analyze_str='' THEN NULL ELSE :last_analyze_str::timestamptz END, "
    " :total_bytes, :table_bytes, :index_bytes)"
)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def collect_pg_table_stats(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
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
            "last_vacuum_str": _str(rec[8]),
            "last_analyze_str": _str(rec[9]),
            "total_bytes": _long(rec[10]),
            "table_bytes": _long(rec[11]),
            "index_bytes": _long(rec[12]),
        }
        cache_execute(INSERT_SQL, params)
        inserted += 1

    return {"cluster_id": cluster_id, "tables_collected": inserted}
