"""schema_snapshots producer: the missing half of get_schema_diff /
get_schema_history / diagnose_root_cause's schema_change signal.

BYTE-IDENTICAL COPIES (edit together; parity-tested):
  data-pipeline/etl_collector/collectors/schema_snapshot.py
  data-pipeline/rds_direct_collector/schema_snapshot.py
The rds_direct copy only ever calls the MySQL entry point (RDS MySQL over direct
TCP through MySQLDataApiAdapter); the unused PG constant rides along so the two
files stay diff-clean, exactly like mysql_table_stats.py.

POSTGRESQL ONLY, AND THAT IS A REFUSAL RATHER THAN A GAP
`collect_mysql_schema_snapshot` reads nothing and writes nothing. MySQL's
information_schema is privilege-filtered in every bucket this collector derives a
diff from, and its scope key (CURRENT_USER()) does not move when a grant does, so a
REVOKE and a DROP are the SAME read: the measured numbers are on that function. Both
MySQL callers (Aurora MySQL here, RDS MySQL in rds_direct_collector) keep calling
it, so the refusal lands in the ETL result instead of a source silently going dark,
and every reader surfaces it through observed() as `unsupported_engine` rather than
as an empty success.

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
(The MySQL counterpart of that read is gone, see below. Its own trap, for whoever
ever revives it: NOT GROUP_CONCAT, whose group_concat_max_len defaults to 1024 and
truncates silently, which is the phantom-DROP bug in a different costume.)

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
    scope, and every READER selects its pair through SCOPED_ROWS in
    schema_diff_util.py, so no cross-scope diff and no cross-scope drop can be
    produced by anybody, by either half of the old defect. A read under another
    scope therefore does not have to be refused: it baselines under its own scope
    (NULL diff, nothing diffed across the two) and says so as `rescoped`.
      PostgreSQL  current_database() || '/' || its pg_database.oid. The name
                  alone would make another cluster's same-named database look
                  comparable. A physical restore preserves the oid (same data,
                  legitimately comparable); a separately created database has a
                  different one.
      MySQL       NONE THAT WORKS, which is why MySQL is not collected. The read
                  is filtered by the reading identity's grants and CURRENT_USER()
                  does not change when those grants do, so the scope cannot
                  separate a REVOKE from a DROP. Measured on 9.3.0; see
                  collect_mysql_schema_snapshot.
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
  R3 the stored history carries  per-schema below scope_status rescoped
     ANY other known scope                       (this read is authoritative
                                                  GOING FORWARD; the other scope's
                                                  rows are never compared against
                                                  and its table-holding schemas
                                                  land in not_seen)
  R4 scope known, cluster has    per-schema below scope_status adopted
     no scoped history yet
  R5 every known stored scope    per-schema below scope_status matched
     is this one

R3 USED TO FREEZE THE CLUSTER and that was a defect, not caution. The freeze
existed for one stated reason: api/dashboard/handler.py recomputed its own
base-vs-latest blob diff with NO notion of read_scope, so a single cross-scope row
in front of it produced the phantom mass drop. Every reader now selects its pair
through SCOPED_ROWS in schema_diff_util.py, so a second scope in the table cannot
be compared against the first by anyone, and the freeze bought nothing while
costing everything: MEASURED on PostgreSQL 14.18, pre-v27 history plus one read of
the wrong database left the cluster on scope_status scope_mismatch with
snapshots_written 0 FOREVER, so the phantom drop the readers were already
reporting could never heal, not even after the operator fixed the config. The
current read's scope is now authoritative going forward, which self-heals: point
the collector back and its own scope is established again on the next cycle.

PER SCHEMA (under R3/R4/R5, i.e. any read that reported a scope)
  S1 same tables                 nothing + heartbeat        unchanged
  S2 different tables            change row                 changes
  S3 NO row of this schema under  baseline row (NULL diff)   baselines
     this scope yet (the comparison
     partner is the latest SAME-SCOPE
     row, never the cross-scope one)
  S4 named, ZERO tables, had some change row, dropped=all    emptied (+changes)
  S5 aggregate came back NULL     nothing + heartbeat        unreadable
  S6 holds tables, NOT named      nothing, NO heartbeat      not_seen
  S7 same tables, but the schema's baseline row (NULL diff)   baselines
     NEWEST row over all scopes is
     not under this one
S1 and S7 are the same OBSERVATION and different WRITES, and the difference is
which row the readers resolve as current. A heartbeat stamps the latest SAME-SCOPE
row (SEEN_SQL), so under S7 the foreign row stays newest and OBSERVED_SQL keeps
resolving the schema to unknown_scope / unmigrated for as long as nothing changes.
S7 writes one NULL-diff row to re-establish the newest row under this scope. S2
covers the case where something DID change since the last comparable read, and it
files a real diff whatever scope the newest row carries: that diff is against the
same-scope predecessor, so it is a fact about ONE catalog.
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
  * MySQL, all of it. A database-level REVOKE hides the database from
    information_schema.schemata (identical to DROP DATABASE), a table-level REVOKE
    hides individual tables from information_schema.tables (identical to DROP
    TABLE, landing in an ordinary S2 `dropped` list) and a column-level GRANT
    shortens the column list (identical to ALTER TABLE DROP COLUMN, landing in
    `modified`). CURRENT_USER() is byte-identical across all three, so no scope key
    separates them. NOT closable with a predicate, so MySQL is REFUSED instead:
    collect_mysql_schema_snapshot writes nothing and every reader reports
    `unsupported_engine`. Measured on 9.3.0.
    PostgreSQL is NOT exposed to this: measured on 14.18, an unprivileged role gets
    the identical PG_SCHEMA_SQL result as the superuser (billing/1, core/2,
    public/1) while `SELECT FROM core.users` raises permission denied.
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
    from collectors.schema_diff_util import (
        ALL_ROWS,
        LATEST_SCOPED_TIME_SUBQUERY,
        SCOPED_ROWS,
        compute_diff,
        diff_is_empty,
        parse_tables,
    )
except ImportError:  # rds_direct_collector: flat asset root
    from schema_diff_util import (
        ALL_ROWS,
        LATEST_SCOPED_TIME_SUBQUERY,
        SCOPED_ROWS,
        compute_diff,
        diff_is_empty,
        parse_tables,
    )

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

# THERE IS NO MySQL CATALOG READ HERE ANY MORE, and its absence is the point. It
# used to select from information_schema.columns / .tables / .schemata with
# CURRENT_USER() as the scope, and every one of those is privilege-filtered while
# CURRENT_USER() is not: see collect_mysql_schema_snapshot below for the measured
# numbers and the refusal. Do not restore a MySQL read without a scope key that
# provably moves when visibility moves; without one, the stored diff reports a
# permission change as dropped tables to five readers.

# COMPARABILITY, not just recency: a blob captured under another scope is not this
# read's previous state, so it must not be diffed against. No match -> baseline.
# `read_scope = :read_scope` also excludes the NULL-scope rows written before
# schema_v27, which is deliberate: their scope is unknown, so they are not
# comparable either, and the first scope-known read re-baselines the schema once.
PREV_SQL = (
    "SELECT tables_json::text " + SCOPED_ROWS +
    "  AND schema_name = :schema_name "
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
    "  " + ALL_ROWS +
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
    "  AND snapshot_time = " + LATEST_SCOPED_TIME_SUBQUERY
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

    # R3. The read says where it was, and that is either the ground the stored
    # history was recorded from or it is not. It is REPORTED and it does NOT stop
    # the cycle.
    #
    # The previous pass froze the cluster here, writing nothing at all, and stated
    # exactly one reason: api/dashboard/handler.py recomputed its own base-vs-latest
    # blob diff with no notion of read_scope, so one cross-scope row in front of it
    # produced the phantom mass drop. Every reader now selects its pair through
    # SCOPED_ROWS, so no two scopes can be compared against each other by anybody,
    # and the freeze only made the damage permanent: MEASURED, pre-v27 history plus
    # one read of the wrong database left this cluster on snapshots_written 0
    # forever, so the drop the readers were already reporting could not heal even
    # after the operator fixed the config.
    #
    # EVERY known scope in the history, not just the newest: what matters is that
    # some stored row was recorded elsewhere, and the operator needs to be told.
    foreign = sorted({sc for sc, _holds in stored.values() if sc and sc != read_scope})
    if foreign:
        out["scope_status"] = "rescoped"
        print(f"[{cluster_id}] schema_snapshot: this read covered '{read_scope}' but part "
              f"of the stored history was recorded from {foreign}. The two are NOT "
              "comparable, so nothing is diffed across them: this read baselines under "
              "its own scope and the other scope's schemas are reported as unconfirmed "
              "until a read reaches them again. If this is not the database you meant "
              "to collect, point the collector back and the next cycle picks the "
              "original history up again")
    elif not any(sc for sc, _holds in stored.values()):
        # R4. First read under a known scope (a new cluster, or a cluster whose
        # only rows predate schema_v27). Everything baselines under this scope.
        out["scope_status"] = "adopted"

    for schema_name, after in sorted(seen.items()):
        # THE COMPARISON PARTNER IS THE LATEST SAME-SCOPE ROW, which is what
        # SCOPED_ROWS exists for. PREV_SQL is built from it and already orders by
        # snapshot_time DESC, so one query answers "the newest row of this schema
        # that this read is comparable to".
        #
        # It used to be gated on `prev_scope == read_scope`, where prev_scope came
        # from the CROSS-SCOPE latest row, and the gate lost real DDL: after ONE
        # cycle read another catalog, the newest row is under that other scope, so a
        # genuine change landing on the next same-scope cycle was stored as a
        # BASELINE with a NULL diff even though a same-scope predecessor was sitting
        # right there. MEASURED on PostgreSQL 14.18, a real CREATE TABLE after one
        # wrong-database cycle: `{"snapshots_written": 2, "baselines": 2,
        # "changes": 0}`, and then the product answered the same question two ways,
        # because the three REPLAY consumers read the stored diff and the two
        # RECOMPUTE consumers recompute it:
        #   get_schema_history  count 1  (only the OLDER event; the new one is gone)
        #   get_schema_diff     added 1  app [invoices]
        # The gate's stated reason was a newer NULL-scope row from a rolling deploy.
        # That row is not comparable to anything by construction (`read_scope =
        # :read_scope` never matches NULL), so it cannot be a partner either way;
        # skipping the query for it only threw away the partner that WAS comparable.
        #
        # TWO DIFFERENT ROWS MATTER HERE and conflating them is what the gate did:
        #   prev_raw     the latest SAME-SCOPE row: what the diff is computed against.
        #   newest_scope the scope of the schema's latest row over ALL scopes: what
        #                every READER resolves as the schema's current state and
        #                confirmation (OBSERVED_SQL takes the newest row per schema
        #                whatever scope it carries). If that row is not under this
        #                scope, this read has to become the newest row or the readers
        #                keep reporting the schema as unconfirmed off a row nothing
        #                will ever confirm again.
        prev_raw = _prev_blob(cache_execute, cluster_id, schema_name, read_scope)
        newest_scope = stored.get(schema_name, ("", False))[0]
        if prev_raw is None:
            diff_json = ""  # -> NULL: baseline, not a change (S3)
            out["baselines"] += 1
        else:
            # Compare the PARSED maps, never the raw text: MySQL's JSON_ARRAYAGG
            # has no ORDER BY, so the same schema can serialize its column arrays
            # in a different order run to run. parse_tables sorts.
            diff = compute_diff(parse_tables(prev_raw), after)
            if diff_is_empty(diff) and newest_scope == read_scope:
                out["unchanged"] += 1  # S1
                _heartbeat(cache_execute, out, cluster_id, schema_name, read_scope, when)
                continue
            if diff_is_empty(diff):
                # S7. Nothing CHANGED since the last comparable read, and the row the
                # readers call latest was recorded somewhere else (another scope, or
                # none at all: a pre-v27 row, or a rolling deploy's old Lambda writing
                # after a new one). A heartbeat cannot fix that: SEEN_SQL stamps the
                # latest SAME-SCOPE row, so the foreign row stays newest and
                # OBSERVED_SQL keeps resolving the schema to unknown_scope /
                # unmigrated forever. One NULL-diff row re-establishes the newest row
                # under this scope, and there is genuinely no change to describe, so
                # NULL is the honest diff.
                diff_json = ""
                out["baselines"] += 1
            else:
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
    """REFUSED, and this is the ONE guard rather than one per caller.

    Both Aurora MySQL (etl_collector) and RDS MySQL (rds_direct_collector) route
    through here, so the refusal is where every caller already goes. It reads no
    catalog and writes nothing at all: the return value is what the ETL result
    reports, so the operator sees a REFUSAL rather than a source that silently went
    dark.

    WHY, measured on MySQL 9.3.0 with the catalog read this function used to run,
    executed as the collecting identity (full numbers in the shared contract's
    snapshot_dialect_supported):

      DROP TABLE dropdb.users     -> dropdb  table_count 0, tables_json {}
      REVOKE SELECT ON appdb.*    -> appdb   the table VANISHES from the read
      GRANT SELECT (id) ON t      -> appdb   the COLUMN vanishes from the read
      REVOKE the whole database   -> appdb   absent from the read entirely
      read_scope, in every case   -> collector@localhost, IDENTICAL

    So tightening the collector user to least privilege is byte-identical to a DBA
    dropping every table, in all three diff buckets, and CURRENT_USER() does not
    move when it happens. There is no scope key here that changes with visibility,
    and a stored diff is consumed by five readers that would each report it as DDL.
    A tool that says "not supported for this engine" has never caused a wrong
    action; a phantom DROP has. PostgreSQL is unaffected: pg_namespace and pg_class
    are not privilege-filtered (measured on 14.18, an unprivileged role gets the
    identical read as the superuser).
    """
    print(f"[{cluster_id}] schema_snapshot: not collected for MySQL. "
          "information_schema is privilege-filtered, so a REVOKE and a DROP produce "
          "the identical read and CURRENT_USER() does not change between them. "
          "Collecting would let a permission change be reported as dropped tables")
    return {
        "cluster_id": cluster_id,
        "read_scope": "",
        "scope_status": "unsupported_dialect",
        "schemas_named": 0,
        "schemas_seen": 0,
        "snapshots_written": 0,
        "baselines": 0,
        "changes": 0,
        "emptied": 0,
        "unchanged": 0,
        "unreadable": 0,
        "heartbeats": 0,
        "not_seen": 0,
        "not_seen_schemas": [],
    }
