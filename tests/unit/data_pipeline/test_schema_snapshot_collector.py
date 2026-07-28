"""schema_snapshot collector behaviour that does not need a live server.

The real-engine coverage (PG catalog SQL, the migration, the INSERT, the readers)
is in test_schema_snapshot_real_pg.py. This file covers what that one cannot:
the MySQL dialect's shape, the 1-MiB-response reasoning, and the rds_instance
dispatch wiring.
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "data-pipeline" / "etl_collector"))

from collectors.schema_snapshot import (  # noqa: E402
    MYSQL_SCHEMA_SQL,
    PG_SCHEMA_SQL,
    collect_mysql_schema_snapshot,
)


class _FakeTarget:
    """rds-data shape. Returns one row per schema, which is the WHOLE point of
    aggregating server-side."""

    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    def execute_statement(self, **kw):
        self.sql = kw["sql"]
        return {"records": [
            [{"stringValue": s}, {"longValue": n}, {"stringValue": blob}]
            for s, n, blob in self.rows
        ]}


class _FakeCache:
    """Two SELECTs to answer: the per-schema previous blob (PREV_SQL) and the
    tracked-schema list (TRACKED_SQL), which is what makes a schema that
    DISAPPEARED from the catalog noticeable at all. `tracked` defaults to the keys
    of `prev`, which is what the real TRACKED_SQL returns for a cluster whose
    stored blobs are all non-empty."""

    def __init__(self, prev=None, tracked=None):
        self.prev = prev or {}
        self.tracked = list(self.prev) if tracked is None else list(tracked)
        self.writes = []

    def __call__(self, sql, params):
        if "schema_name" not in params:
            return {"records": [[{"stringValue": s}] for s in self.tracked]}
        if sql.strip().upper().startswith("SELECT"):
            blob = self.prev.get(params["schema_name"])
            if blob is None:
                return {"records": []}
            return {"records": [[{"stringValue": blob}]]}
        self.writes.append(params)
        return {"records": []}


def _run(rows, prev=None, tracked=None):
    target, cache = _FakeTarget(rows), _FakeCache(prev, tracked)
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


def test_target_read_carries_the_etl_audit_marker():
    _, _, target = _run([("appdb", 1, '{"t": ["id"]}')])
    assert target.sql.startswith("/* source=dbops-etl */")


# ===========================================================================
# Store-on-change
# ===========================================================================


def test_first_snapshot_is_a_baseline_with_no_diff():
    out, cache, _ = _run([("appdb", 2, '{"users": ["id"], "orders": ["id"]}')])
    assert out == {"cluster_id": "rds-mysql-1", "schemas_seen": 1,
                   "snapshots_written": 1, "baselines": 1, "unchanged": 0,
                   "vanished": 0, "vanished_unconfirmed": 0}
    assert len(cache.writes) == 1
    # Empty string -> NULLIF -> NULL. Never a fabricated "everything was added".
    assert cache.writes[0]["diff_json"] == ""
    assert json.loads(cache.writes[0]["tables_json"]) == {"orders": ["id"], "users": ["id"]}


def test_unchanged_schema_writes_nothing():
    out, cache, _ = _run(
        [("appdb", 1, '{"users": ["email", "id"]}')],
        prev={"appdb": '{"users": ["email", "id"]}'})
    assert out["snapshots_written"] == 0
    assert out["unchanged"] == 1
    assert cache.writes == []


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


def test_empty_blob_row_is_skipped_not_stored_as_an_empty_schema():
    """A NULL/empty aggregate must not be stored: '{}' against a real previous
    blob would report every table in the schema as DROPPED."""
    out, cache, _ = _run([("appdb", 0, "")], prev={"appdb": '{"users": ["id"]}'})
    assert out["snapshots_written"] == 0
    assert cache.writes == []


# ===========================================================================
# A schema that goes to zero tables, and a schema that disappears entirely
# ===========================================================================


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
    out, cache, _ = _run([("appdb", 0, "{}")], prev={"appdb": '{"users": ["id"]}'})
    assert out["snapshots_written"] == 1
    assert cache.writes[0]["tables_json"] == "{}"
    assert json.loads(cache.writes[0]["diff_json"])["dropped"] == ["users"]


def test_a_schema_absent_from_the_catalog_is_recorded_as_dropped():
    """DROP SCHEMA / DROP DATABASE: there is no row to iterate, so it can only be
    noticed by comparing the tracked set against what the read returned."""
    out, cache, _ = _run(
        [("appdb", 1, '{"users": ["id"]}')],
        prev={"appdb": '{"users": ["id"]}', "gone": '{"only_table": ["id"]}'})
    assert out["vanished"] == 1
    assert out["vanished_unconfirmed"] == 0
    assert len(cache.writes) == 1
    assert cache.writes[0]["schema_name"] == "gone"
    assert cache.writes[0]["tables_json"] == "{}"
    assert json.loads(cache.writes[0]["diff_json"])["dropped"] == ["only_table"]


def test_a_catalog_read_that_returned_nothing_never_invents_a_mass_drop():
    """Zero schemas returned is what a wrong database, a dead session or a total
    privilege loss looks like, and it is indistinguishable from "every schema was
    dropped". Recording drops from it would be worse than the bug it fixes."""
    out, cache, _ = _run([], prev={"appdb": '{"users": ["id"]}', "other": '{"t": ["id"]}'})
    assert out["vanished"] == 0
    assert out["vanished_unconfirmed"] == 2
    assert cache.writes == []


def test_a_schema_whose_aggregate_came_back_null_is_not_treated_as_dropped():
    """The row was RETURNED, only its blob was unusable: "we do not know" must not
    become a DROP claim."""
    out, cache, _ = _run(
        [("appdb", 0, ""), ("other", 1, '{"t": ["id"]}')],
        prev={"appdb": '{"users": ["id"]}', "other": '{"t": ["id"]}'})
    assert out["vanished"] == 0
    assert out["vanished_unconfirmed"] == 0
    assert cache.writes == []


def test_an_already_empty_schema_is_not_rewritten_every_run():
    """288 runs a day: once a schema's emptiness is recorded, re-recording it
    would file a change row every 5 minutes. TRACKED_SQL excludes a schema whose
    latest blob is already '{}', and the diff guard covers the rest."""
    out, cache, _ = _run([("appdb", 1, '{"users": ["id"]}')],
                         prev={"appdb": '{"users": ["id"]}', "gone": "{}"},
                         tracked=["appdb"])
    assert out["vanished"] == 0
    assert cache.writes == []


def test_tracked_sql_excludes_schemas_whose_latest_blob_is_empty():
    from collectors.schema_snapshot import TRACKED_SQL

    assert "DISTINCT ON (schema_name)" in TRACKED_SQL
    assert "tables_json <> '{}'::jsonb" in TRACKED_SQL
    # Names only. Prefetching every blob at once could exceed the cache's own
    # 1 MiB Data API response on a cluster with several large schemas.
    assert "SELECT schema_name FROM (" in TRACKED_SQL


def test_insert_uses_nullif_not_case_for_the_null_diff():
    """PostgreSQL constant-folds the cast in the untaken CASE branch, so
    `CASE WHEN :d = '' THEN NULL ELSE :d::jsonb END` raises on every baseline.
    Regression guard on a bug the real-engine test caught."""
    from collectors.schema_snapshot import INSERT_SQL

    assert "NULLIF(:diff_json, '')::jsonb" in INSERT_SQL
    assert "CASE WHEN :diff_json" not in INSERT_SQL
    assert "ON CONFLICT (cluster_id, schema_name, snapshot_time) DO NOTHING" in INSERT_SQL


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
