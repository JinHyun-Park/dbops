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
  is written. compute_diff derives `dropped` from set difference, so a truncating
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

=============================================================================
NOTHING HERE INFERS A DROP FROM ABSENCE. WHY THAT IS THE DESIGN, NOT A GAP.
=============================================================================
Four consecutive passes tried to keep the inference "this schema is absent from
the catalog read, therefore its tables were dropped" and make it safe with one
more predicate computed from inside that same read:

  pass 1  no guard at all
  pass 2  `if absent and not returned` (the read returned no row at all).
          UNREACHABLE on PostgreSQL: pg_namespace has `public` in every
          database, so a successful read is never empty.
  pass 3  same guard, made reachable and then re-broken in the same commit.
  pass 4  `corroborated = any(seen.get(s) for s in tracked)`, i.e. at least one
          tracked schema came back holding a table. Satisfied by `public`, which
          on PostgreSQL exists in every database and normally HOLDS TABLES, so a
          read that landed in the WRONG DATABASE corroborated itself.

MEASURED against PostgreSQL 14.18 on the pass-4 code, a cluster collected from
`rightdb` (core, billing, public) read once against `sampledb` whose `public`
holds one ordinary table:
  {"corroborated": true, "schemas_seen": 1, "snapshots_written": 3,
   "vanished": 2, "vanished_unconfirmed": 0}
  get_schema_diff      status ok, totals dropped 4
                       core [orders, users], billing [invoices], public [audit]
  get_schema_history   count 3
  diagnose_root_cause  3 DDL signals examined, skipped []
That read ALSO filed a change row for `public` with dropped [audit] / added
[app_settings], which is not the absence inference at all: it is the ORDINARY
diff path comparing two different databases' catalogs. No predicate on absence
could ever have covered that half.

A read cannot separate "this schema is gone" from "my read could not see it"
using only what is inside the read. So this collector does two things instead:

 1. THE READ REPORTS ITS OWN SCOPE and the scope is stored with the snapshot
    (schema_v27 `read_scope`). Two snapshots are COMPARABLE only under the same
    scope; a read under any other scope writes NOTHING, so no cross-scope diff
    and no cross-scope drop can be produced, by either half of the old defect.
      PostgreSQL  current_database() || '/' || its pg_database.oid. The name
                  alone would make another cluster's same-named database look
                  comparable. A physical restore preserves the oid (same data,
                  legitimately comparable); a separately created database has a
                  different one.
      MySQL       CURRENT_USER(). information_schema there is server-wide, so
                  the connected database is NOT the visibility scope; what the
                  read can see is decided by the reading identity's grants.
 2. ABSENCE IS NEVER A DROP. A tracked schema the read does not name records
    nothing and is reported as `not_seen`, an UNKNOWN that both readers surface
    (`observation.unconfirmed_schemas`). The cost is stated plainly: a genuine
    DROP SCHEMA / DROP DATABASE is no longer reported as a drop. It surfaces as
    "last confirmed at T, not seen since", which is the strongest claim the data
    supports, and this product does not state a negative its data cannot support.
    A drop is only ever recorded from a DIRECT observation: the schema comes back
    from a scope-matching read carrying an EMPTY table set (row S4).

WHY last_seen_at EXISTS. Under store-on-change, snapshot_time is when a schema
last CHANGED, which for a stable schema is months ago, so it cannot distinguish
"unchanged for months" from "not seen for months". That distinction is precisely
the state the four previous passes each resolved to "dropped". It is now a stored
fact: every scope-matching read stamps last_seen_at on the schema's latest row
(an UPDATE, not a row: the blob is TOASTed out of line, so the tuple rewrite is
~100 bytes and the TOAST chain is reused).

=============================================================================
EVERY STATE, AND WHAT IT LOOKS LIKE FROM OUTSIDE
=============================================================================
READ LEVEL (whole cycle)
  R1 the read RAISED            nothing written  caller's schema_snapshot_error
                                                 (no dict is returned at all)
  R2 the read returned NO ROW   nothing written  scope_status scope_unknown
  R3 the stored history carries  nothing written  scope_status scope_mismatch
     ANY other known scope                       (not one baseline either: a
                                                  cross-scope row would make the
                                                  dashboard panel's own
                                                  base-vs-latest blob diff report
                                                  the mass drop this collector
                                                  refuses to)
  R4 scope known, cluster has    per-schema below scope_status adopted
     no scoped history yet
  R5 every known stored scope    per-schema below scope_status matched
     is this one

PER SCHEMA (only under R4/R5)
  S1 same tables                 nothing + heartbeat        unchanged
  S2 different tables            change row                 changes
  S3 latest row is not under      baseline row (NULL diff)   baselines
     this scope (or there is none)
  S4 named, ZERO tables, had some change row, dropped=all    emptied (+changes)
  S5 aggregate came back NULL     nothing + heartbeat        unreadable
  S6 holds tables, NOT named      nothing, NO heartbeat      not_seen
Row S4 is the ONLY path from "no tables" to a recorded drop, and it is a direct
observation under a verified scope: the catalog says this schema exists here and
holds nothing (the read is driven off pg_namespace / information_schema.schemata,
not off the table list). S5 is an empty STRING, not '{}': the aggregate returned
NULL, i.e. we do not know, and "we do not know" may not become a DROP claim.
R1 is why there is no row for a PARTIAL read: both transports assemble the whole
result before the collector sees any of it (the Data API builds the response
server-side; shared/mysql_direct.py builds `records` from one cur.fetchall()), so
a session that dies mid-read raises instead of returning a short row set. Driven
on a real server: a read against a nonexistent database raised and the snapshot
count was identical before and after.

STATES THAT REMAIN GENUINELY INDISTINGUISHABLE. All of them land in S6 or in a
reader caveat, never in a resolved answer:
  * DROP SCHEMA (PG) / DROP DATABASE (MySQL) vs a read that could not reach the
    schema -> S6, reported as unknown, NOT as a drop. This is the deliberate cost.
  * MySQL only: a REVOKE of every privilege on a database hides it from
    information_schema.schemata, which is byte-identical to DROP DATABASE -> S6.
    PostgreSQL is NOT exposed to this: measured, an unprivileged role gets the
    identical PG_SCHEMA_SQL result as the superuser (billing/1, core/2, public/1)
    while `SELECT FROM core.users` raises permission denied.
  * MySQL only: a table-level REVOKE hides individual tables from
    information_schema.tables, which is byte-identical to DROP TABLE, so it lands
    in an ordinary S2 diff's `dropped` list. Not closable without a privilege
    probe; both readers therefore carry a standing caveat on every `dropped`
    list saying it is relative to what the collecting identity could see.
  * a schema whose only history predates schema_v27 (read_scope NULL) is not
    comparable to anything: named -> S3, one re-baseline, once. NOT named -> S6
    like any other, because the not-seen set is taken from the latest stored row
    per schema whatever scope it carries; the readers additionally report it off a
    NULL last_seen_at. Nothing is deleted and nothing is claimed.
  * a registry edit that repoints a registered cluster_id at a DIFFERENT server
    whose database has the same name AND the same oid. Not distinguishable here,
    and not specific to this collector: every producer in the product keys its
    history on cluster_id.
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
# LEFT JOIN gives that schema a row carrying '{}', which is the DIRECT
# OBSERVATION row S4 rests on.
#
# THE 4TH COLUMN IS THE READ'S OWN SCOPE. current_database() alone would treat
# another cluster's same-named database as comparable history; the oid is
# preserved by a physical restore (same data) and differs for a separately
# created database. It is a scalar subquery with no outer column reference, so it
# is legal beside the GROUP BY and PostgreSQL evaluates it once.
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
                '{}'::jsonb)::text AS tables_json,
       (SELECT current_database() || '/' || d.oid::text
          FROM pg_database d WHERE d.datname = current_database()) AS read_scope
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
#
# SCOPE IS CURRENT_USER(), NOT DATABASE(). This catalog is server-wide: the read
# returns every database the connection can see whatever it is connected to, so
# the connected database says nothing about what the read covered. What DOES
# filter it is privileges, and those hang off the reading identity. CURRENT_USER()
# is also stable across a failover and across a db_name config change, where
# @@server_uuid and DATABASE() would each churn the scope and abandon comparable
# history for no reason.
MYSQL_SCHEMA_SQL = """
SELECT t.TABLE_SCHEMA AS schema_name,
       COUNT(*) AS table_count,
       JSON_OBJECTAGG(t.TABLE_NAME, t.cols) AS tables_json,
       CURRENT_USER() AS read_scope
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
SELECT s.SCHEMA_NAME AS schema_name, 0 AS table_count, JSON_OBJECT() AS tables_json,
       CURRENT_USER() AS read_scope
FROM information_schema.schemata s
WHERE s.SCHEMA_NAME NOT IN ('mysql', 'performance_schema', 'information_schema', 'sys')
  AND NOT EXISTS (
    SELECT 1 FROM information_schema.tables tb2
    WHERE tb2.TABLE_SCHEMA = s.SCHEMA_NAME AND tb2.TABLE_TYPE = 'BASE TABLE'
  )
ORDER BY schema_name
"""

# COMPARABILITY, not just recency: a blob captured under another scope is not this
# read's previous state, so it must not be diffed against. No match -> baseline.
# `read_scope = :read_scope` also excludes the NULL-scope rows written before
# schema_v27, which is deliberate: their scope is unknown, so they are not
# comparable either, and the first scope-known read re-baselines the schema once.
PREV_SQL = (
    "SELECT tables_json::text FROM schema_snapshots "
    "WHERE cluster_id = :cluster_id AND schema_name = :schema_name "
    "  AND read_scope = :read_scope "
    "ORDER BY snapshot_time DESC LIMIT 1"
)

# The latest row per schema for this cluster: its scope, and whether it is still
# serving tables to the readers. Two things come out of one statement:
#   * the cluster's ESTABLISHED scope (the newest known one), which decides
#     whether this read is comparable at all;
#   * which schemas would keep serving tables if this read does not name them,
#     which is what `not_seen` reports as an unknown.
# Names and flags only, no blobs: prefetching every blob could blow the cache's
# own 1 MiB Data API response on a cluster with several 2,000-table schemas.
# 'y'/'n' rather than a boolean column on purpose: the Data API hands a boolean
# back as booleanValue and a text column as stringValue, and every other field
# this collector reads goes through _str.
LATEST_SQL = (
    "SELECT schema_name, read_scope, "
    "       CASE WHEN tables_json <> '{}'::jsonb THEN 'y' ELSE 'n' END AS holds_tables "
    "FROM ("
    "  SELECT DISTINCT ON (schema_name) schema_name, read_scope, tables_json, snapshot_time"
    "  FROM schema_snapshots WHERE cluster_id = :cluster_id"
    "  ORDER BY schema_name, snapshot_time DESC"
    ") latest ORDER BY snapshot_time DESC"
)

# ON CONFLICT DO NOTHING: two runs landing on the same run_ts (a retry) must not
# raise. diff_from_previous_json stays NULL for the FIRST snapshot of a schema
# under a scope: a baseline is not a change, and inventing a diff against nothing
# would report every existing table as newly ADDED.
#
# NULLIF(...)::jsonb, NOT `CASE WHEN :diff_json = '' THEN NULL ELSE :diff_json::jsonb END`.
# PostgreSQL constant-folds the cast in the branch it does NOT take, so the CASE
# form raises `invalid input syntax for type json` on every baseline insert.
# Caught by the real-engine test; a mock cache would have passed it forever.
#
# last_seen_at is stamped from the same run timestamp: an inserted row was, by
# construction, observed in this cycle.
INSERT_SQL = (
    "INSERT INTO schema_snapshots "
    "(cluster_id, snapshot_time, schema_name, tables_json, diff_from_previous_json, "
    " read_scope, last_seen_at) "
    "VALUES (:cluster_id, :snapshot_time::timestamptz, :schema_name, :tables_json::jsonb, "
    " NULLIF(:diff_json, '')::jsonb, :read_scope, :snapshot_time::timestamptz) "
    "ON CONFLICT (cluster_id, schema_name, snapshot_time) DO NOTHING"
)

# HEARTBEAT. The schema was named by a scope-matching read and nothing changed, so
# there is no row to write and there IS a fact to record: it still exists. Without
# it, "unchanged since March" and "not seen since March" are the same two rows in
# the table, which is the ambiguity every previous pass resolved to "dropped".
# Scope-filtered like PREV_SQL: a read under another scope confirms nothing, and
# matching 0 rows there is exactly the intended outcome.
SEEN_SQL = (
    "UPDATE schema_snapshots SET last_seen_at = :snapshot_time::timestamptz "
    "WHERE cluster_id = :cluster_id AND schema_name = :schema_name "
    "  AND read_scope = :read_scope "
    "  AND snapshot_time = (SELECT MAX(x.snapshot_time) FROM schema_snapshots x "
    "                       WHERE x.cluster_id = :cluster_id "
    "                         AND x.schema_name = :schema_name "
    "                         AND x.read_scope = :read_scope)"
)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _prev_blob(cache_execute, cluster_id, schema_name, read_scope):
    """Latest stored blob for this (cluster, schema) UNDER THIS SCOPE, or None when
    there is none. None means BASELINE, and it is deliberately distinct from '{}'
    (a real, empty schema)."""
    resp = cache_execute(PREV_SQL, {"cluster_id": cluster_id, "schema_name": schema_name,
                                    "read_scope": read_scope})
    records = (resp or {}).get("records") or []
    if not records or not records[0]:
        return None
    return _str(records[0][0])


def _stored_state(cache_execute, cluster_id):
    """{schema_name: (scope, holds_tables)} from the latest row of each schema.

    The scope is "" for rows written before schema_v27: unknown, therefore not
    comparable, but also not a reason to freeze the cluster out of collection
    forever, so the first read under a known scope adopts it.
    """
    resp = cache_execute(LATEST_SQL, {"cluster_id": cluster_id})
    state = {}
    for rec in ((resp or {}).get("records") or []):
        if not rec:
            continue
        name, scope, holds = _str(rec[0]), _str(rec[1]), _str(rec[2])
        state[name] = (scope, holds == "y")
    return state


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
    # PASS 1 reads the catalog rows and decides nothing. What the WHOLE read said,
    # including the scope it says it covered, has to be known before anything may
    # be written.
    seen = {}  # schema_name -> parsed table map, readable rows only (S1..S4)
    named = set()  # every schema the catalog NAMED, unreadable aggregate included
    read_scope = ""
    unreadable = 0
    for rec in resp.get("records", []):
        # rec[1] is table_count. Nothing reads it: the stored blob is the parsed
        # map and its len() is the honest count. It stays in both statements
        # because rec[2] and rec[3] are positional.
        schema_name = _str(rec[0])
        raw = _str(rec[2])
        if not read_scope and len(rec) > 3:
            read_scope = _str(rec[3])
        if schema_name:
            named.add(schema_name)
        if not schema_name or not raw:
            # S5. Empty STRING, not '{}'. '{}' means "read fine, no tables" and IS
            # stored (that is the emptied-schema observation); "" means the
            # aggregate returned NULL, i.e. we do not know, and inventing a mass
            # DROP out of "we do not know" is worse than storing nothing. A schema
            # we could not read is also not a schema that is gone, so it stays in
            # `named` and out of the not-seen set.
            unreadable += 1
            continue
        seen[schema_name] = parse_tables(raw)

    stored = _stored_state(cache_execute, cluster_id)
    out = {
        "cluster_id": cluster_id,
        "read_scope": read_scope,
        "scope_status": "matched",
        "schemas_named": len(named),
        "schemas_seen": len(seen),
        "snapshots_written": 0,
        "baselines": 0,
        "changes": 0,
        "emptied": 0,
        "unchanged": 0,
        "unreadable": unreadable,
        "heartbeats": 0,
        "not_seen": 0,
        "not_seen_schemas": [],
    }

    # R2. No row means no scope, and with no scope nothing in this read can be
    # compared to anything stored. PostgreSQL only reaches this by having no
    # non-system schema at all; MySQL reaches it when the identity can see no
    # database. Either way: write nothing, and let the readers report the cluster
    # as unconfirmed off the last_seen_at that stops advancing.
    if not read_scope:
        out["scope_status"] = "scope_unknown"
        print(f"[{cluster_id}] schema_snapshot: the catalog read named no schema and "
              "reported no scope, so nothing was compared or written")
        return out

    # R3. THE guard the four previous passes were reaching for, and it is not a
    # predicate on the read's contents: the read says where it was, and that is
    # either the ground the stored history was recorded from or it is not.
    #
    # NOT ONE BASELINE is written on a mismatch either, and that is measured, not
    # cautious: the dashboard panel (api/dashboard/handler.py
    # _SCHEMA_SNAPSHOT_PAIRS_SQL) recomputes its own diff from the OLDEST and
    # NEWEST blob per schema and has no notion of scope. Driven with this branch
    # disabled, one baseline row written from the wrong database made that panel
    # report dropped [(public, audit)] and get_schema_diff totals dropped 1.
    #
    # EVERY known scope in the history, not just the newest: with two scopes
    # present, taking the newest would re-baseline the other one's schemas under
    # this scope and hand the panel that same cross-scope pair. This collector
    # never writes a second scope, so the set is normally empty or a single match;
    # checking all of it keeps that an invariant rather than an assumption.
    foreign = sorted({sc for sc, _holds in stored.values() if sc and sc != read_scope})
    if foreign:
        out["scope_status"] = "scope_mismatch"
        out["not_seen_schemas"] = sorted(s for s, (sc, holds) in stored.items() if holds)
        out["not_seen"] = len(out["not_seen_schemas"])
        print(f"[{cluster_id}] schema_snapshot: this read covered '{read_scope}' but the "
              f"stored history was recorded from {foreign}, so the two are not "
              f"comparable and NOTHING was written ({out['not_seen']} schema(s) left "
              "unconfirmed). Point the collector back at the database the history "
              "came from, or delete this cluster's schema_snapshots rows to "
              "re-baseline against the new one")
        return out
    if not any(sc for sc, _holds in stored.values()):
        # R4. First read under a known scope (a new cluster, or a cluster whose
        # only rows predate schema_v27). Everything baselines under this scope.
        out["scope_status"] = "adopted"

    for schema_name, after in sorted(seen.items()):
        # The comparison partner is the schema's LATEST row, and only if that row
        # was captured under this scope. Asking PREV_SQL for the newest row that
        # merely HAPPENS to carry this scope would compare against a row that is
        # not the current state whenever a newer row exists without one, which a
        # rolling deploy can produce (an old Lambda version writing after a new
        # one). No comparable partner means baseline, never a diff.
        prev_scope = stored.get(schema_name, ("", False))[0]
        prev_raw = (_prev_blob(cache_execute, cluster_id, schema_name, read_scope)
                    if prev_scope == read_scope else None)
        if prev_raw is None:
            diff_json = ""  # -> NULL: baseline, not a change (S3)
            out["baselines"] += 1
        else:
            # Compare the PARSED maps, never the raw text: MySQL's JSON_ARRAYAGG
            # has no ORDER BY, so the same schema can serialize its column arrays
            # in a different order run to run. parse_tables sorts.
            diff = compute_diff(parse_tables(prev_raw), after)
            if diff_is_empty(diff):
                out["unchanged"] += 1  # S1
                _heartbeat(cache_execute, out, cluster_id, schema_name, read_scope, when)
                continue
            out["changes"] += 1  # S2
            if not after:
                # S4, the only path from "no tables" to a recorded drop. The
                # catalog named this schema under a scope that matches the stored
                # history's, and reported it holds nothing. That is an
                # observation, not an inference from absence.
                out["emptied"] += 1
                print(f"[{cluster_id}] schema_snapshot: '{schema_name}' exists in "
                      f"'{read_scope}' and holds no table; recording "
                      f"{len(diff['dropped'])} dropped table(s) from that direct "
                      "observation")
            diff_json = json.dumps(diff)

        cache_execute(INSERT_SQL, {
            "cluster_id": cluster_id,
            "snapshot_time": when,
            "schema_name": schema_name,
            # Re-serialize from the parsed+sorted map so the stored blob is
            # canonical and the next run's text comparison is stable.
            "tables_json": json.dumps(after),
            "diff_json": diff_json,
            "read_scope": read_scope,
        })
        out["snapshots_written"] += 1
        print(f"[{cluster_id}] schema_snapshot stored for '{schema_name}' "
              f"({len(after)} tables, {'baseline' if not diff_json else 'change'}, "
              f"scope '{read_scope}')")

    # S5's heartbeat: named but unusable blob. Existence under this scope IS
    # confirmed; only the content is not, and snapshot_time already dates that.
    for schema_name in sorted(named - set(seen)):
        _heartbeat(cache_execute, out, cluster_id, schema_name, read_scope, when)

    # S6. The schemas the readers are still serving tables for that this read did
    # not name. NOTHING is written and nothing is concluded: a DROP SCHEMA and a
    # read that could not reach the schema produce the identical evidence here, so
    # this is reported as an unknown and both readers surface it as one.
    out["not_seen_schemas"] = sorted(s for s, (sc, holds) in stored.items()
                                     if holds and s not in named)
    out["not_seen"] = len(out["not_seen_schemas"])
    if out["not_seen"]:
        print(f"[{cluster_id}] schema_snapshot: {out['not_seen']} schema(s) "
              f"{out['not_seen_schemas']} were not named by this read of "
              f"'{read_scope}' and are left as-is. They are reported as "
              "unconfirmed, NOT as dropped: absence cannot tell a DROP SCHEMA "
              "apart from a read that did not reach them")
    return out


def _heartbeat(cache_execute, out, cluster_id, schema_name, read_scope, when):
    """Record that this schema was still there. The counter is statements ISSUED,
    not rows matched: a schema the read named for the first time has no row under
    this scope yet, so its UPDATE matches nothing and its INSERT (which stamps
    last_seen_at itself) is what records the observation."""
    cache_execute(SEEN_SQL, {"cluster_id": cluster_id, "schema_name": schema_name,
                             "read_scope": read_scope, "snapshot_time": when})
    out["heartbeats"] += 1


def collect_pg_schema_snapshot(rds_data_client, cache_execute, target_cluster_arn,
                               target_secret_arn, cluster_id, database, snapshot_ts=None):
    return _collect(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn,
                    cluster_id, database, PG_SCHEMA_SQL, snapshot_ts)


def collect_mysql_schema_snapshot(rds_data_client, cache_execute, target_cluster_arn,
                                  target_secret_arn, cluster_id, database, snapshot_ts=None):
    return _collect(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn,
                    cluster_id, database, MYSQL_SCHEMA_SQL, snapshot_ts)
