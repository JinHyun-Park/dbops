"""schema_snapshot collector behaviour that does not need a live server.

The real-engine coverage (PG catalog SQL, the migration, the INSERT, the readers)
is in test_schema_snapshot_real_pg.py. This file covers what that one cannot:
the MySQL dialect's shape, the 1-MiB-response reasoning, the rds_instance
dispatch wiring, and one driven row per state in the module's state matrix.
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "data-pipeline" / "etl_collector"))

from collectors.schema_snapshot import (  # noqa: E402
    INSERT_SQL,
    LATEST_SQL,
    MYSQL_SCHEMA_SQL,
    PG_SCHEMA_SQL,
    PREV_SQL,
    SEEN_SQL,
    collect_mysql_schema_snapshot,
)

# The scope a read reports. On MySQL this is CURRENT_USER(); the value is opaque
# to the collector, only its equality with the stored one decides anything.
_SCOPE = "dbops@%"
_OTHER_SCOPE = "readonly@10.0.0.1"


class _FakeTarget:
    """rds-data shape. One row per schema, which is the whole point of aggregating
    server-side, plus the 4th column every row carries: the scope the read itself
    reports."""

    def __init__(self, rows, scope=_SCOPE):
        self.rows = rows
        self.scope = scope
        self.sql = None

    def execute_statement(self, **kw):
        self.sql = kw["sql"]
        return {"records": [
            [{"stringValue": s}, {"longValue": n},
             ({"isNull": True} if blob is None else {"stringValue": blob}),
             {"stringValue": self.scope}]
            for s, n, blob in self.rows
        ]}


class _FakeCache:
    """Answers the collector's three cache reads and records its two writes.

    `prev` is the stored blob per schema, ALL of it recorded under `stored_scope`
    (None models rows written before schema_v27, whose scope is unknown). PREV_SQL
    is scope-filtered in the shipped SQL, so this fake refuses to hand a blob to a
    read from another scope: that is the behaviour under test, not a convenience.
    """

    def __init__(self, prev=None, stored_scope=_SCOPE):
        self.prev = prev or {}
        self.stored_scope = stored_scope
        self.writes = []
        self.heartbeats = []

    def __call__(self, sql, params):
        head = sql.strip().split()[0].upper()
        if head == "SELECT":
            if "schema_name" in params:  # PREV_SQL
                if params.get("read_scope") != self.stored_scope:
                    return {"records": []}
                blob = self.prev.get(params["schema_name"])
                if blob is None:
                    return {"records": []}
                return {"records": [[{"stringValue": blob}]]}
            # LATEST_SQL: the latest row per schema, its scope, and whether it is
            # still serving tables to the readers.
            scope_field = ({"isNull": True} if self.stored_scope is None
                           else {"stringValue": self.stored_scope})
            return {"records": [
                [{"stringValue": s}, scope_field,
                 {"stringValue": "y" if blob not in ("", "{}") else "n"}]
                for s, blob in self.prev.items()
            ]}
        if head == "UPDATE":  # SEEN_SQL heartbeat
            self.heartbeats.append(params["schema_name"])
            return {"records": []}
        self.writes.append(params)
        return {"records": []}


def _run(rows, prev=None, scope=_SCOPE, stored_scope=_SCOPE):
    target, cache = _FakeTarget(rows, scope), _FakeCache(prev, stored_scope)
    out = collect_mysql_schema_snapshot(
        target, cache, "arn:x", "arn:y", "rds-mysql-1", "appdb",
        snapshot_ts="2026-07-10T00:00:00+00:00")
    return out, cache, target


# ===========================================================================
# Dialect SQL shape
# ===========================================================================


def test_mysql_sql_aggregates_and_avoids_the_truncating_functions():
    # JSON_OBJECTAGG has no length cap. GROUP_CONCAT silently truncates at
    # group_concat_max_len=1024, which would make compute_diff report tables that
    # merely fell off the end as DROPPED.
    assert "JSON_OBJECTAGG" in MYSQL_SCHEMA_SQL
    assert "JSON_ARRAYAGG" in MYSQL_SCHEMA_SQL
    assert "GROUP_CONCAT" not in MYSQL_SCHEMA_SQL
    # No LIMIT anywhere: server-side aggregation is all-or-nothing, so a schema
    # too big for the 1 MiB Data API response errors out and writes NOTHING
    # rather than writing a partial snapshot that looks like a mass DROP.
    assert "LIMIT" not in MYSQL_SCHEMA_SQL.upper()
    assert "LIMIT" not in PG_SCHEMA_SQL.upper()
    # Views are not tables.
    assert "'BASE TABLE'" in MYSQL_SCHEMA_SQL
    assert "relkind IN ('r', 'p')" in PG_SCHEMA_SQL


def test_both_dialects_make_the_read_report_its_own_scope():
    """The whole fix rests on the read naming the catalog it reached, so that a
    later absence is interpreted against it instead of being guessed at from
    inside the read."""
    assert "AS read_scope" in PG_SCHEMA_SQL
    assert "current_database()" in PG_SCHEMA_SQL
    # The oid, not just the name: another cluster's same-named database is not
    # comparable history, and a physical restore keeps the oid so it stays one.
    assert "pg_database" in PG_SCHEMA_SQL and "d.oid" in PG_SCHEMA_SQL
    # MySQL's information_schema is server-wide, so the connected database is not
    # the visibility scope; privileges are, and they hang off the identity.
    assert MYSQL_SCHEMA_SQL.count("CURRENT_USER() AS read_scope") == 2  # both UNION halves
    assert "DATABASE() AS read_scope" not in MYSQL_SCHEMA_SQL


def test_comparability_is_scope_filtered_in_every_statement_that_decides_it():
    assert "read_scope = :read_scope" in PREV_SQL
    assert "read_scope = :read_scope" in SEEN_SQL
    assert "read_scope" in INSERT_SQL and "last_seen_at" in INSERT_SQL
    # LATEST_SQL deliberately does NOT filter by scope: it has to report what the
    # cluster's ESTABLISHED scope is, which is the thing being compared.
    assert ":read_scope" not in LATEST_SQL
    assert "DISTINCT ON (schema_name)" in LATEST_SQL


def test_target_read_carries_the_etl_audit_marker():
    _, _, target = _run([("appdb", 1, '{"t": ["id"]}')])
    assert target.sql.startswith("/* source=dbops-etl */")


# ===========================================================================
# Store-on-change
# ===========================================================================


def test_first_snapshot_is_a_baseline_with_no_diff():
    out, cache, _ = _run([("appdb", 2, '{"users": ["id"], "orders": ["id"]}')])
    assert out == {"cluster_id": "rds-mysql-1", "read_scope": _SCOPE,
                   "scope_status": "adopted", "schemas_named": 1, "schemas_seen": 1,
                   "snapshots_written": 1, "baselines": 1, "changes": 0, "emptied": 0,
                   "unchanged": 0, "unreadable": 0, "heartbeats": 0,
                   "not_seen": 0, "not_seen_schemas": []}
    assert len(cache.writes) == 1
    # Empty string -> NULLIF -> NULL. Never a fabricated "everything was added".
    assert cache.writes[0]["diff_json"] == ""
    assert cache.writes[0]["read_scope"] == _SCOPE
    assert json.loads(cache.writes[0]["tables_json"]) == {"orders": ["id"], "users": ["id"]}


def test_unchanged_schema_writes_nothing_but_records_that_it_was_seen():
    out, cache, _ = _run(
        [("appdb", 1, '{"users": ["email", "id"]}')],
        prev={"appdb": '{"users": ["email", "id"]}'})
    assert out["snapshots_written"] == 0
    assert out["unchanged"] == 1
    assert cache.writes == []
    # THE new fact. Without it, "unchanged since March" and "not seen since March"
    # are the same two rows in the table, which is the ambiguity every previous
    # pass resolved to "dropped".
    assert cache.heartbeats == ["appdb"]


def test_column_reordering_alone_is_not_a_change():
    """MySQL's JSON_ARRAYAGG has no ORDER BY, so the same schema serializes its
    column arrays in whatever order the engine felt like. Comparing raw TEXT
    would write a fake change row on a random subset of the 288 daily runs."""
    out, cache, _ = _run(
        [("appdb", 1, '{"users": ["id", "email"]}')],
        prev={"appdb": '{"users": ["email", "id"]}'})
    assert out["unchanged"] == 1
    assert cache.writes == []


def test_real_change_stores_the_diff_in_compute_diff_shape():
    out, cache, _ = _run(
        [("appdb", 2, '{"users": ["email", "id", "phone"], "audit": ["id"]}')],
        prev={"appdb": '{"users": ["email", "id"], "legacy": ["k"]}'})
    assert out["snapshots_written"] == 1
    assert out["changes"] == 1
    assert out["baselines"] == 0
    diff = json.loads(cache.writes[0]["diff_json"])
    assert diff["added"] == ["audit"]
    assert diff["dropped"] == ["legacy"]
    assert diff["modified"] == [
        {"table": "users", "added_columns": ["phone"], "dropped_columns": []}
    ]
    assert diff["rename_candidates"] == []


def test_multiple_schemas_are_tracked_independently():
    out, cache, _ = _run(
        [("a", 1, '{"t1": ["id"]}'), ("b", 1, '{"t2": ["id", "new"]}')],
        prev={"a": '{"t1": ["id"]}', "b": '{"t2": ["id"]}'})
    assert out["schemas_seen"] == 2
    assert out["unchanged"] == 1
    assert out["snapshots_written"] == 1
    assert cache.writes[0]["schema_name"] == "b"
    assert cache.heartbeats == ["a"]


def test_empty_blob_row_is_skipped_not_stored_as_an_empty_schema():
    """A NULL/empty aggregate must not be stored: '{}' against a real previous
    blob would report every table in the schema as DROPPED."""
    out, cache, _ = _run([("appdb", 0, None)], prev={"appdb": '{"users": ["id"]}'})
    assert out["snapshots_written"] == 0
    assert out["unreadable"] == 1
    assert cache.writes == []
    # Named by the catalog, so its EXISTENCE was confirmed; only the content was
    # not, and snapshot_time already dates that.
    assert cache.heartbeats == ["appdb"]


def test_mysql_sql_also_returns_databases_that_hold_no_table():
    """Grouping the table list by schema returns NO ROW for a database with zero
    tables, and the collector can only diff the schemas the read returned, so
    dropping the LAST table used to make the whole database invisible while its
    stale blob stood as `latest` forever.

    The statement itself was executed against a real MySQL 9.3.0 rather than only
    asserted on as text: appdb with 2 tables + 1 view returned
    {"users": [...], "orders": [...]} with the view excluded, emptydb returned
    '{}', and after dropping every table in appdb it returned appdb -> '{}',
    which is the whole point of the UNION ALL half. A MySQL fixture is
    deliberately NOT added to this suite: it costs a ~40s server start on every
    run and the structural assertions below fail if the half is removed.
    """
    assert "information_schema.schemata" in MYSQL_SCHEMA_SQL
    assert "UNION ALL" in MYSQL_SCHEMA_SQL
    assert "JSON_OBJECT() AS tables_json" in MYSQL_SCHEMA_SQL
    # Not a LEFT JOIN: JSON_OBJECTAGG rejects a NULL member name
    # (ER_JSON_DOCUMENT_NULL_KEY), so an outer-joined empty row would abort the
    # whole statement instead of aggregating to an empty object.
    assert "NOT EXISTS" in MYSQL_SCHEMA_SQL
    assert "pg_namespace" in PG_SCHEMA_SQL  # the PG counterpart, real-engine tested


def test_zero_table_schema_is_stored_as_an_empty_map_with_the_drop_diff():
    """The ONE path from "no tables" to a recorded drop, and it is a direct
    observation under a matching scope: the catalog named this schema here and
    reported it holds nothing."""
    out, cache, _ = _run([("appdb", 0, "{}")], prev={"appdb": '{"users": ["id"]}'})
    assert out["snapshots_written"] == 1
    assert out["emptied"] == 1
    assert cache.writes[0]["tables_json"] == "{}"
    assert json.loads(cache.writes[0]["diff_json"])["dropped"] == ["users"]


def test_a_schema_absent_from_the_catalog_records_nothing_and_is_reported_unknown():
    """THE fix. DROP SCHEMA / DROP DATABASE leaves no row to iterate, and so does
    a read that could not reach the schema, so absence cannot tell them apart. It
    is no longer resolved to either: nothing is written and the schema is reported
    as not_seen, which both readers surface as an unknown.

    The cost is stated out loud: a genuine DROP SCHEMA is no longer reported as a
    drop. It is reported as "last confirmed at T, not seen since"."""
    out, cache, _ = _run(
        [("appdb", 1, '{"users": ["id"]}')],
        prev={"appdb": '{"users": ["id"]}', "gone": '{"only_table": ["id"]}'})
    assert cache.writes == []
    assert out["not_seen"] == 1
    assert out["not_seen_schemas"] == ["gone"]
    assert out["heartbeats"] == 1 and cache.heartbeats == ["appdb"]


def test_a_catalog_read_that_returned_nothing_never_invents_a_mass_drop():
    """Zero rows means no scope either, so nothing in the read can be compared to
    anything stored."""
    out, cache, _ = _run([], prev={"appdb": '{"users": ["id"]}', "other": '{"t": ["id"]}'})
    assert out["scope_status"] == "scope_unknown"
    assert out["read_scope"] == ""
    assert cache.writes == [] and cache.heartbeats == []


# ===========================================================================
# THE CRITICAL, five passes running: a read that landed somewhere else.
# ===========================================================================


def test_a_read_from_another_scope_never_compares_across_it_and_never_freezes():
    """MEASURED on real PostgreSQL 14.18 against the pass-4 code: a cluster
    collected from `rightdb` (core, billing, public) read once against `sampledb`
    whose `public` holds one ordinary table returned
      {"corroborated": true, "schemas_seen": 1, "snapshots_written": 3,
       "vanished": 2, "vanished_unconfirmed": 0}
    and get_schema_diff reported dropped 4: core [orders, users], billing
    [invoices], public [audit]. `public` exists in every PostgreSQL database and
    normally HOLDS TABLES, so the pass-4 corroboration predicate was satisfied by
    the wrong database itself.

    Note the shape: `public` came back with DIFFERENT tables, not with none, so that
    half of the damage was an ORDINARY diff and no predicate on ABSENCE could ever
    have covered it. What covers both halves is comparability, and comparability is
    now decided in the READERS' selection (SCOPED_ROWS), not by refusing to write.

    THE PASS-5 FREEZE IS GONE, and that is a fix and not a relaxation. It wrote
    nothing at all on a mismatch, for one stated reason: the dashboard panel
    recomputed its own base-vs-latest blob diff with no notion of scope. That
    reader is now scope-filtered, and the freeze had a cost that was measured:
    pre-v27 history plus one wrong-database read left the cluster on
    snapshots_written 0 FOREVER, so the phantom drop the readers were already
    reporting could not heal even after the operator fixed the config.

    What must hold instead: nothing is DIFFED across the two scopes. Every write
    here is a baseline with a NULL diff, and the schemas of the other scope are
    reported unconfirmed.
    """
    out, cache, _ = _run(
        [("public", 1, '{"app_settings": ["k", "v"]}')],
        prev={"public": '{"audit": ["id"]}', "core": '{"users": ["id"]}',
              "billing": '{"invoices": ["id"]}'},
        scope=_OTHER_SCOPE, stored_scope=_SCOPE)
    assert out["scope_status"] == "rescoped"
    # NOT ONE DIFF across the scopes: every write is a baseline, NULL diff.
    assert out["changes"] == 0
    assert all(w["diff_json"] == "" for w in cache.writes), cache.writes
    assert [w["schema_name"] for w in cache.writes] == ["public"]
    assert all(w["read_scope"] == _OTHER_SCOPE for w in cache.writes)
    # ...and the schemas this read could not reach are unconfirmed, not dropped.
    assert out["not_seen"] == 2
    assert out["not_seen_schemas"] == ["billing", "core"]


def test_a_rescope_writes_only_baselines_so_no_cross_scope_diff_can_exist():
    """The property that replaces the freeze, stated as the invariant it is: after a
    rescope every stored row of the NEW scope is a baseline, so the readers'
    scope-filtered pair has nothing to diff yet and CANNOT produce a drop. A future
    edit that lets a mismatched read carry a diff over from the old scope fails
    here."""
    out, cache, _ = _run(
        [("public", 1, '{"app_settings": ["k"]}'), ("brandnew", 1, '{"t": ["id"]}')],
        prev={"public": '{"audit": ["id"]}'},
        scope=_OTHER_SCOPE, stored_scope=_SCOPE)
    assert out["scope_status"] == "rescoped"
    assert out["baselines"] == 2 and out["changes"] == 0
    assert all(w["diff_json"] == "" for w in cache.writes), cache.writes


def test_history_whose_scope_is_unknown_is_re_baselined_not_diffed():
    """Rows written before schema_v27 carry no scope, so they are not comparable
    to anything. They must not freeze the cluster out of collection forever
    either: the first read under a known scope ADOPTS it and re-baselines, once.

    Mutation check on PREV_SQL's scope filter: with the filter removed the fake
    hands over the legacy blob and this comes back as a CHANGE with a diff."""
    out, cache, _ = _run(
        [("appdb", 2, '{"users": ["id"], "audit": ["id"]}')],
        prev={"appdb": '{"users": ["id"]}'}, stored_scope=None)
    assert out["scope_status"] == "adopted"
    assert out["baselines"] == 1 and out["changes"] == 0
    assert cache.writes[0]["diff_json"] == ""  # NULL diff: a baseline, not a DDL claim
    assert cache.writes[0]["read_scope"] == _SCOPE


def test_a_legacy_schema_the_read_does_not_name_is_still_reported_unknown():
    """Its stored row has no scope, so it can never be compared; it must not
    silently keep serving tables as if current either."""
    out, cache, _ = _run(
        [("appdb", 1, '{"users": ["id"]}')],
        prev={"appdb": '{"users": ["id"]}', "ghost": '{"gone": ["id"]}'},
        stored_scope=None)
    assert out["not_seen_schemas"] == ["ghost"]
    assert all(w["schema_name"] != "ghost" for w in cache.writes)


# ===========================================================================
# THE STATE MATRIX. Every state a read, and every state a TRACKED schema (one the
# readers are currently serving tables for), can be reported as.
#
# The point of the table is that no two rows produce the same observable outcome,
# because the defect this surface keeps relocating is exactly two different states
# collapsing into one indistinguishable output. R1 (the read raised) is not here:
# the exception leaves _collect entirely and the caller records
# schema_snapshot_error, which test_schema_snapshot_real_pg.py drives against a
# real server.
# ===========================================================================

_USERS = '{"users": ["id"]}'
_OTHER = '{"t": ["id"]}'

# row, label, catalog rows, previous blobs, read scope, stored scope,
# expected writes as (schema, blob-is-empty), expected counters
_MATRIX = [
    ("R2", "the read returned no row", [], {"appdb": _USERS}, _SCOPE, _SCOPE,
     [], {"scope_status": "scope_unknown", "read_scope": ""}),
    # R3 no longer freezes: it baselines under its own scope and reports the other
    # scope's table-holding schemas as unconfirmed. Nothing is DIFFED across the two
    # (baselines only), and the readers never select a cross-scope pair.
    ("R3", "the read covered another scope", [("appdb", 1, _OTHER)], {"appdb": _USERS},
     _OTHER_SCOPE, _SCOPE, [("appdb", False)],
     # not_seen is 0 here BECAUSE the read named appdb: its fresh baseline under the
     # new scope becomes its latest row, so the readers confirm it and serve nothing
     # stale. A schema of the old scope that this read does NOT name is the not_seen
     # case, driven by
     # test_a_read_from_another_scope_never_compares_across_it_and_never_freezes.
     {"scope_status": "rescoped", "baselines": 1, "changes": 0, "not_seen": 0}),
    ("R4", "no scoped history yet", [("appdb", 1, _USERS)], {}, _SCOPE, _SCOPE,
     [("appdb", False)], {"scope_status": "adopted", "baselines": 1}),
    ("S1", "same tables", [("appdb", 1, _USERS)], {"appdb": _USERS}, _SCOPE, _SCOPE,
     [], {"scope_status": "matched", "unchanged": 1, "heartbeats": 1}),
    ("S2", "different tables", [("appdb", 2, '{"users": ["id"], "audit": ["id"]}')],
     {"appdb": _USERS}, _SCOPE, _SCOPE, [("appdb", False)],
     {"changes": 1, "snapshots_written": 1, "emptied": 0}),
    ("S3", "a schema with no history under this scope",
     [("appdb", 1, _USERS), ("fresh", 1, _OTHER)], {"appdb": _USERS}, _SCOPE, _SCOPE,
     [("fresh", False)], {"baselines": 1, "unchanged": 1, "scope_status": "matched"}),
    ("S4", "named, exists, ZERO tables", [("appdb", 0, "{}")], {"appdb": _USERS},
     _SCOPE, _SCOPE, [("appdb", True)], {"emptied": 1, "changes": 1}),
    ("S5", "aggregate came back NULL", [("appdb", 0, None), ("other", 1, _OTHER)],
     {"appdb": _USERS, "other": _OTHER}, _SCOPE, _SCOPE, [],
     {"unreadable": 1, "heartbeats": 2, "not_seen": 0}),
    ("S6", "holds tables, NOT named by the read", [("other", 1, _OTHER)],
     {"appdb": _USERS, "other": _OTHER}, _SCOPE, _SCOPE, [],
     {"not_seen": 1, "unchanged": 1, "heartbeats": 1}),
]

_SIGNATURE_KEYS = ("scope_status", "snapshots_written", "baselines", "changes",
                   "emptied", "unchanged", "unreadable", "heartbeats", "not_seen")


def test_state_matrix_every_row_is_driven_and_distinguishable():
    seen_signatures = {}
    for row, label, rows, prev, scope, stored, want_writes, want in _MATRIX:
        out, cache, _ = _run(rows, prev=prev, scope=scope, stored_scope=stored)
        # (schema, is the stored blob empty) is what the readers turn into "these
        # tables are gone", so that is what the matrix pins.
        got = sorted((w["schema_name"], w["tables_json"] == "{}") for w in cache.writes)
        assert got == sorted(want_writes), (row, label, got)
        for key, value in want.items():
            assert out[key] == value, (row, label, key, out)
        sig = (tuple(got),) + tuple(out[k] for k in _SIGNATURE_KEYS)
        assert sig not in seen_signatures, (
            f"row {row} ({label}) is indistinguishable from row "
            f"{seen_signatures[sig][0]} ({seen_signatures[sig][1]}): {sig}")
        seen_signatures[sig] = (row, label)


def test_no_reachable_read_ever_writes_a_drop_the_catalog_did_not_show():
    """The invariant, driven rather than argued: across every row of the matrix,
    the only write carrying a `dropped` list is the one where the catalog itself
    returned the schema with an empty table set (S4)."""
    dropped_writers = []
    for row, _label, rows, prev, scope, stored, _w, _c in _MATRIX:
        _, cache, _ = _run(rows, prev=prev, scope=scope, stored_scope=stored)
        for w in cache.writes:
            if w["diff_json"] and json.loads(w["diff_json"])["dropped"]:
                dropped_writers.append((row, w["schema_name"], w["tables_json"]))
    assert dropped_writers == [("S4", "appdb", "{}")], dropped_writers


def test_insert_uses_nullif_not_case_for_the_null_diff():
    """PostgreSQL constant-folds the cast in the untaken CASE branch, so
    `CASE WHEN :d = '' THEN NULL ELSE :d::jsonb END` raises on every baseline.
    Regression guard on a bug the real-engine test caught."""
    assert "NULLIF(:diff_json, '')::jsonb" in INSERT_SQL
    assert "CASE WHEN :diff_json" not in INSERT_SQL
    assert "ON CONFLICT (cluster_id, schema_name, snapshot_time) DO NOTHING" in INSERT_SQL


def test_latest_sql_reports_emptiness_as_text_not_as_a_boolean():
    """The Data API hands a boolean column back as booleanValue and a text column
    as stringValue, and every field this collector reads goes through _str. A
    boolean column would read as "" here and silently make every schema look
    empty, i.e. never not_seen."""
    assert "THEN 'y' ELSE 'n'" in LATEST_SQL


# ===========================================================================
# Wiring: both dispatch sites, so the collector cannot ship dark
# ===========================================================================


def test_etl_collector_dispatches_both_dialects():
    src = (_ROOT / "data-pipeline" / "etl_collector" / "handler.py").read_text()
    assert "collect_pg_schema_snapshot(" in src
    assert "collect_mysql_schema_snapshot(" in src
    # The run-wide timestamp, so all schemas of one run share a snapshot_time.
    assert src.count("snapshot_ts=run_ts,\n            )\n        except Exception as e:\n            result[\"schema_snapshot_error\"]") >= 1


def test_rds_direct_collector_dispatches_the_mysql_collector():
    """S6: without this line the `sql`-gated readers PASS an rds_instance cluster
    (sql: True) into a table with no producer for it."""
    src = (_ROOT / "data-pipeline" / "rds_direct_collector" / "handler.py").read_text()
    assert "from schema_snapshot import collect_mysql_schema_snapshot" in src
    assert "collect_mysql_schema_snapshot(\n                adapter, cache_execute" in src


def test_cache_execute_returns_the_response_in_both_handlers():
    """The collector reads its own previous blob back through cache_execute; a
    closure that returns None makes every run look like a baseline and rewrite a
    row 288 times a day."""
    for rel in ("etl_collector", "rds_direct_collector"):
        src = (_ROOT / "data-pipeline" / rel / "handler.py").read_text()
        assert "return cache_rds_data.execute_statement(" in src or \
               "return rds_data.execute_statement(" in src, rel
