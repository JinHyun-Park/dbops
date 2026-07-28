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

A SCHEMA CAN GO TO ZERO, AND THAT IS A CHANGE
Store-on-change means the last blob written for a schema stands as "latest"
until something replaces it, so a schema the collector stops SEEING keeps
serving its dropped tables as existing, forever, on all three readers. Two
different events do that and both are handled, differently on purpose:
  * schema still exists, last table dropped -> the catalog read itself returns
    the schema with '{}' (it is driven off pg_namespace / information_schema.
    schemata, not off the table list). Directly observed, no inference.
  * schema itself dropped -> no row exists to return, so it is inferred from
    absence against TRACKED_SQL.
Both of those are DESTRUCTIVE conclusions: the readers turn them into "these
tables are gone" for a DBA. So both hang off CORROBORATION, below.

CORROBORATION, AND EVERY STATE A TRACKED SCHEMA CAN BE REPORTED AS
A schema is TRACKED when its latest stored blob is non-empty (TRACKED_SQL), i.e.
the readers are serving tables for it right now. CORROBORATED means at least one
TRACKED schema came back from THIS read with a table still in it.
Why that and not "the read returned no rows at all", which is what the previous
version guarded on: PostgreSQL creates `public` in every database and pg_catalog
is world-readable, so a successful read is never empty and that guard could not
fire. MEASURED on PostgreSQL 14.18, a cluster whose history was collected from
`appdb` read once against `sampledb` (which is the literal db_name fallback in
etl_collector/handler.py) reported
  {"schemas_seen": 1, "snapshots_written": 3, "vanished": 2, "vanished_unconfirmed": 0}
and get_schema_diff, get_schema_history and diagnose_root_cause all reported
core dropped [orders, users], billing dropped [invoices], public dropped [audit].
Corroboration is the only evidence available here that the read covered the same
scope the stored history was recorded from, because schema_snapshots has no
database column to compare against (adding one is the upgrade path; it needs a
migration plus all three readers).

  the read said                    corroborated  written              counted as
  ----------------------------------------------------------------------------------
  1 the same tables                 either       nothing              unchanged
  2 different tables                either       change row           snapshots_written
  3 '{}', 2+ tracked schemas        yes          '{}' + dropped diff  snapshots_written
  4 '{}', 1 tracked schema          no           '{}' + dropped diff  uncorroborated_writes
  5 '{}', 2+ tracked schemas        no           NOTHING              emptied_unconfirmed
  6 an empty STRING (NULL agg)      either       nothing              unreadable
  7 nothing at all (absent)         yes          '{}' + dropped diff  vanished
  8 nothing at all (absent)         no           NOTHING              vanished_unconfirmed
  9 the read RAISED                 n/a          NOTHING AT ALL       caller's *_error key
Row 9 is why there is no tenth row for a PARTIAL read: both transports build the
whole result before the collector sees any of it (the Data API assembles the
response server-side; shared/mysql_direct.py builds `records` from a single
cur.fetchall()), so a session that dies mid-read raises instead of returning a
short row set. Driven on a real server: a read against a nonexistent database
raised and the snapshot count was identical before and after.
Row 4 is the one destructive write made without corroboration, and it is a
DIRECT OBSERVATION ("this schema exists here and holds no table") on a cluster
where corroboration is impossible by construction: the only tracked schema is
the one that emptied. Trusting it keeps the single-schema cluster, which is the
common shape, from silently serving dropped tables forever. Row 5 is the same
observation on a cluster that DOES have other tracked schemas and none of them
corroborate, which is what a scope change looks like, so nothing is written.
Rows 4/5/8 are all self-healing: no state is kept, and the next run with one
readable table in any tracked schema records the real thing.
INDISTINGUISHABLE, on purpose and disclosed:
  * a cluster with exactly ONE tracked schema, read against a different database
    that happens to hold a same-named schema. Row 4 writes the drop. `public`
    makes that a certainty rather than a coincidence on PostgreSQL.
  * a scope-drifted read still BASELINES any schema it finds that this cluster
    has never snapshotted. That is not a destructive claim (a baseline carries a
    NULL diff, and every reader reports it as insufficient history rather than as
    a change), so it is deliberately not suppressed.
  * MySQL's information_schema is privilege-filtered, so REVOKE on one database
    reads exactly like DROP DATABASE. PostgreSQL is NOT exposed to this:
    measured, an unprivileged role gets the identical PG_SCHEMA_SQL result as
    the superuser (billing/1, core/2, public/1) while `SELECT FROM core.users`
    raises permission denied.
  ponytail: both need the read to name its own scope (current_database() on PG,
  a SCHEMATA privilege probe on MySQL) AND a database column on schema_snapshots
  to compare it with. Until then, treat a db_name change on a registered cluster
  as a re-baseline, not as DDL.
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
#
# THE DRIVER IS pg_namespace, NOT the table list. Grouping the table list by
# schema returns NO ROW for a schema with zero tables, and _collect can only diff
# the schemas the read returned, so a schema that lost its LAST table used to go
# invisible: its final blob stayed the newest row forever and all three readers
# kept serving the dropped tables as existing. Driving off pg_namespace with a
# LEFT JOIN gives that schema a row carrying '{}', which is a DIRECT OBSERVATION
# ("this schema exists and has no tables") rather than an inference from absence.
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
SELECT ns.nspname AS schema_name,
       COUNT(p.table_name)::bigint AS table_count,
       COALESCE(jsonb_object_agg(p.table_name, p.cols)
                  FILTER (WHERE p.table_name IS NOT NULL),
                '{}'::jsonb)::text AS tables_json
FROM pg_namespace ns
LEFT JOIN per_table p ON p.schema_name = ns.nspname
WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
  AND ns.nspname NOT LIKE 'pg\\_%'
GROUP BY ns.nspname
ORDER BY ns.nspname
"""

# information_schema.columns is the same catalog mysql_table_stats already hits.
# The TABLE_TYPE join excludes views. MySQL has no schema-vs-database split, so
# TABLE_SCHEMA IS the database and lands in schema_name directly.
#
# The UNION ALL half is the MySQL counterpart of the pg_namespace LEFT JOIN
# above: a database that still exists but holds no BASE TABLE gets its '{}' row.
# It is a UNION and not a LEFT JOIN because JSON_OBJECTAGG rejects a NULL member
# name (ER_JSON_DOCUMENT_NULL_KEY), so the outer-joined empty row would abort the
# whole statement instead of aggregating to an empty object.
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
UNION ALL
SELECT s.SCHEMA_NAME AS schema_name, 0 AS table_count, JSON_OBJECT() AS tables_json
FROM information_schema.schemata s
WHERE s.SCHEMA_NAME NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
  AND NOT EXISTS (
    SELECT 1 FROM information_schema.tables tb2
    WHERE tb2.TABLE_SCHEMA = s.SCHEMA_NAME AND tb2.TABLE_TYPE = 'BASE TABLE'
  )
ORDER BY schema_name
"""

PREV_SQL = (
    "SELECT tables_json::text FROM schema_snapshots "
    "WHERE cluster_id = :cluster_id AND schema_name = :schema_name "
    "ORDER BY snapshot_time DESC LIMIT 1"
)

# Schemas this cluster has a NON-EMPTY latest snapshot for. Names only, no blobs:
# prev blobs stay a per-schema fetch so a cluster with several 2,000-table schemas
# cannot blow the cache's own 1 MiB Data API response on the prefetch.
# `tables_json <> '{}'` keeps a long-dropped schema from being re-examined on all
# 288 runs a day forever: once its emptiness is recorded, it is no longer tracked.
TRACKED_SQL = (
    "SELECT schema_name FROM ("
    "  SELECT DISTINCT ON (schema_name) schema_name, tables_json"
    "  FROM schema_snapshots WHERE cluster_id = :cluster_id"
    "  ORDER BY schema_name, snapshot_time DESC"
    ") latest WHERE tables_json <> '{}'::jsonb"
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


def _prev_blob(cache_execute, cluster_id, schema_name):
    """Latest stored blob for this (cluster, schema), or None when the schema has
    never been snapshotted. None means BASELINE, and it is deliberately distinct
    from '{}' (a real, empty schema)."""
    resp = cache_execute(PREV_SQL, {"cluster_id": cluster_id, "schema_name": schema_name})
    records = (resp or {}).get("records") or []
    if not records or not records[0]:
        return None
    return _str(records[0][0])


def _tracked_schemas(cache_execute, cluster_id):
    resp = cache_execute(TRACKED_SQL, {"cluster_id": cluster_id})
    return [_str(rec[0]) for rec in ((resp or {}).get("records") or []) if rec]


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
    unreadable = 0
    # PASS 1 decides nothing. The destructive branches below need to know what
    # the WHOLE read said before any of them may write, which is exactly what the
    # previous version could not do: it wrote each schema as it walked the rows.
    seen = {}  # schema_name -> parsed table map, readable rows only (row 1/2/3/4/5)
    named = set()  # every schema the catalog NAMED, unreadable aggregate included
    for rec in resp.get("records", []):
        # rec[1] is table_count. Nothing reads it: the stored blob is the parsed
        # map and its len() is the honest count. It stays in both statements
        # because rec[2] is positional, so dropping the column moves tables_json.
        schema_name = _str(rec[0])
        raw = _str(rec[2])
        if schema_name:
            named.add(schema_name)
        if not schema_name or not raw:
            # Row 6. Empty STRING, not '{}'. '{}' means "read fine, no tables"
            # and IS stored (that is the dropped-last-table case); "" means the
            # aggregate returned NULL, i.e. we do not know, and inventing a mass
            # DROP out of "we do not know" is worse than storing nothing. A
            # schema we could not read is also not a schema that is gone, so it
            # stays in `named` and out of the vanished pass.
            unreadable += 1
            continue
        seen[schema_name] = parse_tables(raw)

    tracked = set(_tracked_schemas(cache_execute, cluster_id))
    # THE one signal both destructive branches hang off. See the module
    # docstring's state table: `not named` (the previous guard) is unreachable on
    # PostgreSQL because `public` exists in every database, so a successful read
    # is never empty and a wrong-database read looked exactly like a mass DROP.
    corroborated = any(seen.get(s) for s in tracked)
    # Absent = tracked but not even NAMED by the read: DROP SCHEMA (or, on MySQL,
    # DROP DATABASE). Pass 1 cannot cover it, because a dropped schema produces
    # no row to iterate.
    absent = sorted(s for s in tracked if s not in named)
    # Row 4 vs row 5. With a single tracked schema there is nothing left to
    # corroborate WITH, so a direct '{}' observation is trusted and logged as
    # such; with 2+, an emptied schema that nothing corroborates is treated as a
    # scope change and not written.
    trust_empty = corroborated or len(tracked) < 2

    vanished = 0
    vanished_unconfirmed = 0
    emptied_unconfirmed = 0
    uncorroborated_writes = 0

    for schema_name, after in seen.items():
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
                unchanged += 1  # row 1
                continue
            if not after and not trust_empty:
                emptied_unconfirmed += 1  # row 5
                print(f"[{cluster_id}] schema_snapshot: '{schema_name}' came back with "
                      f"0 tables and no other tracked schema of {len(tracked)} still "
                      "holds one, so nothing was stored (a read that lost the scope "
                      "looks exactly like this)")
                continue
            if not after and not corroborated:
                uncorroborated_writes += 1  # row 4
                print(f"[{cluster_id}] schema_snapshot: '{schema_name}' is the only "
                      "tracked schema and came back with 0 tables; recorded as "
                      "dropped on the catalog's word alone (nothing left to "
                      "corroborate the read's scope with)")
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
              f"({len(after)} tables, {'baseline' if not diff_json else 'change'})")

    # Rows 7 and 8. This is the one INFERENCE on the surface, so unlike row 4 it
    # never runs uncorroborated: absence is evidence of a drop only if the read
    # is evidence of anything at all.
    if absent and not corroborated:
        vanished_unconfirmed = len(absent)
        print(f"[{cluster_id}] schema_snapshot: {vanished_unconfirmed} tracked "
              f"schema(s) {absent} absent from the catalog read, but no tracked "
              "schema came back holding a table, so they are left as-is rather "
              "than recorded as dropped (a wrong database, a dead session or a "
              "privilege-filtered catalog is indistinguishable from a mass drop)")
        absent = []
    for schema_name in absent:
        prev_raw = _prev_blob(cache_execute, cluster_id, schema_name)
        diff = compute_diff(parse_tables(prev_raw), {})
        if diff_is_empty(diff):
            continue
        cache_execute(INSERT_SQL, {
            "cluster_id": cluster_id,
            "snapshot_time": when,
            "schema_name": schema_name,
            "tables_json": "{}",
            "diff_json": json.dumps(diff),
        })
        written += 1
        vanished += 1
        print(f"[{cluster_id}] schema_snapshot stored for '{schema_name}' "
              f"(schema absent from the catalog, {len(diff['dropped'])} table(s) dropped)")

    return {
        "cluster_id": cluster_id,
        "schemas_seen": len(seen),
        "snapshots_written": written,
        "baselines": baselines,
        "unchanged": unchanged,
        "unreadable": unreadable,
        "corroborated": corroborated,
        "vanished": vanished,
        "vanished_unconfirmed": vanished_unconfirmed,
        "emptied_unconfirmed": emptied_unconfirmed,
        "uncorroborated_writes": uncorroborated_writes,
    }


def collect_pg_schema_snapshot(rds_data_client, cache_execute, target_cluster_arn,
                               target_secret_arn, cluster_id, database, snapshot_ts=None):
    return _collect(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn,
                    cluster_id, database, PG_SCHEMA_SQL, snapshot_ts)


def collect_mysql_schema_snapshot(rds_data_client, cache_execute, target_cluster_arn,
                                  target_secret_arn, cluster_id, database, snapshot_ts=None):
    return _collect(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn,
                    cluster_id, database, MYSQL_SCHEMA_SQL, snapshot_ts)
