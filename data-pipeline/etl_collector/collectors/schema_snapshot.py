"""schema_snapshots producer: the missing half of get_schema_diff /
get_schema_history / diagnose_root_cause's schema_change signal.

BYTE-IDENTICAL COPIES (edit together; parity-tested):
  data-pipeline/etl_collector/collectors/schema_snapshot.py
  data-pipeline/rds_direct_collector/schema_snapshot.py
The rds_direct copy only ever calls the MySQL entry point (RDS MySQL over direct
TCP through MySQLDataApiAdapter); the unused PG constant rides along so the two
files stay diff-clean, exactly like mysql_table_stats.py.

WHY THE CATALOG READ AGGREGATES SERVER-SIDE
A one-row-per-column projection is the obvious shape and it is the wrong one:
the RDS Data API caps a response at 1 MiB, and that projection measures ~688 KiB
at 500 tables and ~2.75 MiB at 2,000, so it breaks silently on exactly the big
customer schemas that matter. json_object_agg / JSON_OBJECTAGG collapse the
response to ONE ROW PER SCHEMA carrying just the blob: 3.2 KiB measured at 24
tables, ~267 KiB extrapolated at 2,000.
  ponytail: no LIMIT and no truncation marker, on purpose. Server-side
  aggregation is ALL-OR-NOTHING, so there is no partial snapshot to guard
  against: past roughly 7,600 tables in one schema (1 MiB / 137 measured bytes
  per table) the Data API errors, the caller's try/except records it and NOTHING
  is written. compute_diff infers `dropped` from absence, so a truncating
  collector would report tables that merely fell out of the list as DROPPED to
  the DBA (that bug is live today in the dashboard's table_stats LIMIT 100
  panel). A missing snapshot is honest; a phantom DROP is not. If a real schema
  ever exceeds the cap, page the tables into several blobs, do not add a LIMIT.
MySQL trap: NOT GROUP_CONCAT. group_concat_max_len defaults to 1024 and
truncates silently, which is the phantom-DROP bug in a different costume.
JSON_OBJECTAGG (5.7.22+) has no such cap.

STORE-ON-CHANGE, not every run
The ETL runs every STATS_COLLECTION_INTERVAL_MIN (5) minutes = 288 times a day.
Writing every run would store 288 near-identical rows per schema per day that
both change-readers then discard (they filter empty diff_from_previous_json),
and would make get_schema_diff's implicit latest-vs-second-latest comparison
always diff two identical 5-minutes-apart snapshots and answer "no changes".
So: compare against the stored blob and INSERT only on a real difference.
"""

from datetime import datetime, timezone

try:  # etl_collector: package-rooted asset
    from collectors.schema_diff_util import compute_diff, diff_is_empty, parse_tables
except ImportError:  # rds_direct_collector: flat asset root
    from schema_diff_util import compute_diff, diff_is_empty, parse_tables

import json

# pg_catalog, not information_schema: the latter is a slow view stack over the
# same data. relkind r/p covers ordinary + partitioned tables and excludes views,
# indexes, sequences and TOAST. attnum > 0 drops system columns (ctid, xmin);
# NOT attisdropped drops columns removed by ALTER TABLE DROP COLUMN, whose
# pg_attribute rows survive as `........pg.dropped.N........`.
PG_SCHEMA_SQL = """
WITH cols AS (
  SELECT n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p')
    AND a.attnum > 0
    AND NOT a.attisdropped
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg\\_%'
), per_table AS (
  SELECT schema_name, table_name,
         jsonb_agg(column_name ORDER BY column_name) AS cols
  FROM cols
  GROUP BY schema_name, table_name
)
SELECT schema_name,
       COUNT(*)::bigint AS table_count,
       jsonb_object_agg(table_name, cols)::text AS tables_json
FROM per_table
GROUP BY schema_name
ORDER BY schema_name
"""

# information_schema.columns is the same catalog mysql_table_stats already hits.
# The TABLE_TYPE join excludes views. MySQL has no schema-vs-database split, so
# TABLE_SCHEMA IS the database and lands in schema_name directly.
MYSQL_SCHEMA_SQL = """
SELECT t.TABLE_SCHEMA AS schema_name,
       COUNT(*) AS table_count,
       JSON_OBJECTAGG(t.TABLE_NAME, t.cols) AS tables_json
FROM (
  SELECT c.TABLE_SCHEMA, c.TABLE_NAME, JSON_ARRAYAGG(c.COLUMN_NAME) AS cols
  FROM information_schema.columns c
  JOIN information_schema.tables tb
    ON tb.TABLE_SCHEMA = c.TABLE_SCHEMA AND tb.TABLE_NAME = c.TABLE_NAME
  WHERE c.TABLE_SCHEMA NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
    AND tb.TABLE_TYPE = 'BASE TABLE'
  GROUP BY c.TABLE_SCHEMA, c.TABLE_NAME
) t
GROUP BY t.TABLE_SCHEMA
ORDER BY t.TABLE_SCHEMA
"""

PREV_SQL = (
    "SELECT tables_json::text FROM schema_snapshots "
    "WHERE cluster_id = :cluster_id AND schema_name = :schema_name "
    "ORDER BY snapshot_time DESC LIMIT 1"
)

# ON CONFLICT DO NOTHING: two runs landing on the same run_ts (a retry) must not
# raise. diff_from_previous_json stays NULL for the FIRST snapshot of a schema:
# a baseline is not a change, and inventing a diff against nothing would report
# every existing table as newly ADDED.
#
# NULLIF(...)::jsonb, NOT `CASE WHEN :diff_json = '' THEN NULL ELSE :diff_json::jsonb END`.
# PostgreSQL constant-folds the cast in the branch it does NOT take, so the CASE
# form raises `invalid input syntax for type json` on every baseline insert.
# Caught by the real-engine test; a mock cache would have passed it forever.
INSERT_SQL = (
    "INSERT INTO schema_snapshots "
    "(cluster_id, snapshot_time, schema_name, tables_json, diff_from_previous_json) "
    "VALUES (:cluster_id, :snapshot_time::timestamptz, :schema_name, :tables_json::jsonb, "
    " NULLIF(:diff_json, '')::jsonb) "
    "ON CONFLICT (cluster_id, schema_name, snapshot_time) DO NOTHING"
)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    if field.get("isNull"):
        return 0
    v = field.get("longValue")
    if v is not None:
        return v
    dv = field.get("doubleValue")
    if dv is not None:
        return int(dv)
    sv = field.get("stringValue")
    return int(float(sv)) if sv else 0


def _prev_blob(cache_execute, cluster_id, schema_name):
    """Latest stored blob for this (cluster, schema), or None when the schema has
    never been snapshotted. None means BASELINE, and it is deliberately distinct
    from '{}' (a real, empty schema)."""
    resp = cache_execute(PREV_SQL, {"cluster_id": cluster_id, "schema_name": schema_name})
    records = (resp or {}).get("records") or []
    if not records or not records[0]:
        return None
    return _str(records[0][0])


def _collect(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn,
             cluster_id, database, sql, snapshot_ts=None):
    resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {sql}",
        includeResultMetadata=True,
    )

    when = snapshot_ts or datetime.now(timezone.utc).isoformat()
    written = 0
    baselines = 0
    unchanged = 0
    schemas = []
    for rec in resp.get("records", []):
        schema_name = _str(rec[0])
        table_count = _long(rec[1])
        raw = _str(rec[2])
        if not schema_name or not raw:
            continue
        schemas.append(schema_name)
        after = parse_tables(raw)

        prev_raw = _prev_blob(cache_execute, cluster_id, schema_name)
        if prev_raw is None:
            diff_json = ""  # -> NULL: baseline, not a change
            baselines += 1
        else:
            # Compare the PARSED maps, never the raw text: MySQL's JSON_ARRAYAGG
            # has no ORDER BY, so the same schema can serialize its column arrays
            # in a different order run to run. parse_tables sorts.
            diff = compute_diff(parse_tables(prev_raw), after)
            if diff_is_empty(diff):
                unchanged += 1
                continue
            diff_json = json.dumps(diff)

        cache_execute(INSERT_SQL, {
            "cluster_id": cluster_id,
            "snapshot_time": when,
            "schema_name": schema_name,
            # Re-serialize from the parsed+sorted map so the stored blob is
            # canonical and the next run's text comparison is stable.
            "tables_json": json.dumps(after),
            "diff_json": diff_json,
        })
        written += 1
        print(f"[{cluster_id}] schema_snapshot stored for '{schema_name}' "
              f"({table_count} tables, {'baseline' if not diff_json else 'change'})")

    return {
        "cluster_id": cluster_id,
        "schemas_seen": len(schemas),
        "snapshots_written": written,
        "baselines": baselines,
        "unchanged": unchanged,
    }


def collect_pg_schema_snapshot(rds_data_client, cache_execute, target_cluster_arn,
                               target_secret_arn, cluster_id, database, snapshot_ts=None):
    return _collect(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn,
                    cluster_id, database, PG_SCHEMA_SQL, snapshot_ts)


def collect_mysql_schema_snapshot(rds_data_client, cache_execute, target_cluster_arn,
                                  target_secret_arn, cluster_id, database, snapshot_ts=None):
    return _collect(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn,
                    cluster_id, database, MYSQL_SCHEMA_SQL, snapshot_ts)
