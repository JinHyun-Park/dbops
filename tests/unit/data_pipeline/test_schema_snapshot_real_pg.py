"""E-4 REAL-ENGINE test: the collector SQL and both readers' SQL are executed
against a live PostgreSQL server, not a fake that hands back canned rows.

Why this file exists: the previous tier shipped SQL that could never parse,
because the test double returned exactly the rows the assertions compared
against. A mock cannot tell you that `jsonb_object_agg` exists, that
`n.nspname NOT LIKE 'pg\\_%'` escapes the way you think, that a LEFT JOIN on a
ROW_NUMBER CTE returns the baseline row, or that `diff_from_previous_json != '{}'`
implicit-casts. So this test:

  1. initdb's a throwaway PostgreSQL cluster,
  2. applies the REAL migration file data-pipeline/schema_migrator/sql/schema_v26.sql,
  3. creates real source tables and runs the REAL PG_SCHEMA_SQL from the
     collector against the real catalog,
  4. drives the real collector through a Data-API-shaped adapter over that same
     server, so the INSERT/PREV SQL is executed too,
  5. executes the REAL reader SQL strings (schema_diff / schema_history /
     diagnose_root_cause) with the same `:name` binding the RDS Data API uses.

ENGINE: PostgreSQL from the local install (verified against 14.18). Skipped, not
faked, when no initdb/pg_ctl/psql is on the machine.
"""

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SQL_DIR = _ROOT / "data-pipeline" / "schema_migrator" / "sql"
_MIGRATION = _SQL_DIR / "schema_v26.sql"
# v27 adds read_scope + last_seen_at. Applied in the migrator's own order (v26
# then v27) everywhere v26 is, because the shipped collector reads both columns.
_MIGRATION_V27 = _SQL_DIR / "schema_v27.sql"


_COLLECTORS = _ROOT / "data-pipeline" / "etl_collector" / "collectors"


_BASE_SCHEMA = _ROOT / "data-pipeline" / "sql" / "schema.sql"


def _migrate(pg):
    pg.raw(_MIGRATION.read_text())
    pg.raw(_MIGRATION_V27.read_text())
    # cluster_meta, lifted verbatim out of the production base schema. The readers
    # resolve the DIALECT from `cluster_meta.engine` (schema snapshots are
    # PostgreSQL-only: MySQL's catalog is privilege-filtered, so a REVOKE and a DROP
    # are the same read), so without this relation every reader here would report
    # `unavailable` and every assertion below would be about the wrong state.
    src = _BASE_SCHEMA.read_text()
    start = src.index("CREATE TABLE IF NOT EXISTS cluster_meta")
    pg.raw(src[start:src.index(");", start) + 2])


def _meta(pg, cluster_id, engine="aurora-postgresql"):
    """The cluster_meta row the ETL writes BEFORE the snapshot collector runs
    (etl_collector/handler.py collects meta first), so having it wherever snapshots
    exist is what production looks like, not a convenience."""
    pg.raw("INSERT INTO cluster_meta (cluster_id, account_id, region, engine) "
           f"VALUES ('{cluster_id}', '123456789012', 'ap-northeast-2', '{engine}') "
           "ON CONFLICT (cluster_id) DO UPDATE SET engine = EXCLUDED.engine")



sys.path.insert(0, str(_ROOT / "mcp-servers"))
sys.path.insert(0, str(_COLLECTORS.parent))

from collectors.schema_snapshot import (  # noqa: E402
    PG_SCHEMA_SQL,
    collect_pg_schema_snapshot,
)
from mcp_servers.incident.tools import diagnose_root_cause as drc  # noqa: E402
from mcp_servers.operations.tools import schema_diff as sd  # noqa: E402
from mcp_servers.operations.tools import schema_history as sh  # noqa: E402

_SEARCH = [
    "",  # PATH
    "/opt/homebrew/opt/postgresql@14/bin",
    "/opt/homebrew/opt/postgresql@15/bin",
    "/opt/homebrew/opt/postgresql@16/bin",
    "/opt/homebrew/bin",
    "/usr/local/opt/postgresql@14/bin",
    "/usr/lib/postgresql/14/bin",
    "/usr/lib/postgresql/15/bin",
    "/usr/lib/postgresql/16/bin",
]


def _find(exe):
    for d in _SEARCH:
        p = shutil.which(exe, path=d) if d else shutil.which(exe)
        if p:
            return p
    return None


_INITDB, _PGCTL, _PSQL = _find("initdb"), _find("pg_ctl"), _find("psql")
pytestmark = pytest.mark.skipif(
    not (_INITDB and _PGCTL and _PSQL),
    reason="no local PostgreSQL (initdb/pg_ctl/psql), real-engine E-4 test skipped",
)

def _reserve_port():
    """Ask the kernel, and HOLD the port until `pg_ctl start`.

    A hardcoded port collides with a sibling real-PG fixture in the same run and
    with a postmaster an aborted run left behind. Closing the probe socket here
    (the previous `with socket.socket()`) released the port at IMPORT time, so
    between collection and start nothing held it and two modules in one process
    could be handed the same number; the loser's `pg_ctl start` then raises before
    its fixture yields, so the `finally` never runs and its datadir survives.
    Bound-but-not-listening refuses connections, so the hold does not make
    `_serving()` below see a live server.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    return str(s.getsockname()[1]), s


_PORT, _PORT_HOLD = _reserve_port()


def _release_port():
    """Drop the reservation so PostgreSQL can bind. Idempotent."""
    global _PORT_HOLD
    if _PORT_HOLD is not None:
        _PORT_HOLD.close()
        _PORT_HOLD = None
# A unix socket path over ~103 bytes is refused, and the pytest tmp path is much
# longer than that, so the data dir goes somewhere short and we talk TCP.
# PID-scoped: two concurrent runs must not share one datadir, and the teardown of
# one must not delete the datadir the other is still serving from.
_PGDATA = os.path.join(tempfile.gettempdir(), f"dbops_e4_pg_{os.getpid()}")


def _serving(timeout=5.0):
    """Is ANYTHING still answering on this fixture's port?

    Asked instead of trusting pg_ctl, because pg_ctl finds the server through
    `postmaster.pid`: once that file is gone the stop reports nothing useful while the
    postmaster keeps serving. Polled rather than probed once, so a backend that
    outlives the postmaster by a moment is not reported as a live server (a false
    alarm here aborts a module, which is the failure mode that gets guards deleted).
    """
    deadline = time.monotonic() + timeout
    while True:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", int(_PORT))) != 0:
                return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.2)


def _stop_and_remove():
    """Stop FIRST, then remove, and REFUSE TO REMOVE under a live server.

    rmtree under a live postmaster leaves it running on a datadir that no longer
    exists, and every later fixture in that process then fails to start: 34 fixture
    ERRORs once got written off as flake.

    `ignore_errors=True` after an UNCHECKED stop is exactly how that state was
    reached a second time. MEASURED on the shipped version, driving this function
    with a live server whose postmaster.pid had been removed (which is what a
    previous masked rmtree leaves behind): it returned with no exception, the
    postmaster was still alive, still serving the port, and the datadir was gone.
    Half-succeeding silently is worse than failing: the failure lands on whoever runs
    next. So the stop is VERIFIED against the port, and a server that did not stop
    raises HERE, in the fixture that owns it, with its datadir intact so it can still
    be stopped by hand or by the next setup call.
    """
    existed = os.path.isdir(_PGDATA)
    if existed:
        subprocess.run([_PGCTL, "-D", _PGDATA, "-m", "immediate", "stop"],
                       capture_output=True)
    if _serving():
        raise RuntimeError(
            f"something is still serving 127.0.0.1:{_PORT} after pg_ctl stop on "
            f"{_PGDATA}. NOT removing the datadir under a live server: that is what "
            "leaves a postmaster on a datadir that no longer exists and turns every "
            "later fixture in this process into an unrelated ERROR. Stop it by hand: "
            f"{_PGCTL} -D {_PGDATA} -m immediate stop"
        )
    if existed:
        # No ignore_errors: a tree this process owns and no longer serves has to
        # come off cleanly, and a failure here is a fact, not noise to swallow.
        shutil.rmtree(_PGDATA)


@pytest.fixture(scope="module")
def pg():
    _stop_and_remove()
    os.makedirs(_PGDATA, exist_ok=True)
    subprocess.run([_INITDB, "-D", _PGDATA, "-U", "dbops", "--auth=trust"],
                   check=True, capture_output=True)
    _release_port()  # hand the port over to PostgreSQL, last possible moment
    subprocess.run(
        [_PGCTL, "-D", _PGDATA, "-o", f"-p {_PORT} -k {_PGDATA} -c listen_addresses=127.0.0.1",
         "-l", os.path.join(_PGDATA, "log"), "-w", "start"],
        check=True, capture_output=True)
    try:
        yield _Server()
    finally:
        _stop_and_remove()


# `:name` binds, but NOT the `::type` cast that follows one. The lookbehind makes
# the second colon of `::` non-matching, so `:snapshot_a::timestamptz` binds
# snapshot_a and leaves the cast alone.
_BIND = re.compile(r"(?<!:):([a-z_][a-z0-9_]*)")


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


class _Server:
    """Runs SQL through psql. `execute` mimics CacheClient.execute (named binds,
    dict rows); `raw` is for fixture DDL."""

    def raw(self, sql, db="postgres", user="dbops"):
        out = subprocess.run(
            [_PSQL, "-h", "127.0.0.1", "-p", _PORT, "-U", user, "-d", db,
             "-v", "ON_ERROR_STOP=1", "-tA", "-F", "\x1f", "-c", sql],
            capture_output=True, text=True)
        if out.returncode != 0:
            raise AssertionError(f"psql failed: {out.stderr.strip()}\nSQL: {sql}")
        return [ln.split("\x1f") for ln in out.stdout.splitlines() if ln != ""]

    def execute(self, sql, params=None):
        bound = _BIND.sub(lambda m: _lit((params or {})[m.group(1)]), sql)
        # row_to_json gives name-keyed rows the way includeResultMetadata does.
        rows = self.raw(f"SELECT row_to_json(_rj) FROM ({bound}) _rj")
        out = []
        for r in rows:
            row = json.loads(r[0])
            # CacheClient.execute hands a jsonb column back as a STRING (the Data
            # API stringValue branch), so re-stringify: the readers pass these
            # values straight to the agent and the test must see what ships.
            out.append({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                        for k, v in row.items()})
        return _Result(out)


class _Result:
    def __init__(self, rows):
        self.rows = rows
        self.row_count = len(rows)
        self.columns = list(rows[0].keys()) if rows else []


class _DataApi:
    """rds-data client shape over the same server, for the collector's target
    read. Returns the {"records": [[{...field}]]} envelope the collectors unwrap."""

    def __init__(self, server):
        self.s = server

    def execute_statement(self, resourceArn=None, secretArn=None, database=None,
                          sql=None, parameters=None, includeResultMetadata=None):
        bound = sql
        if parameters:
            vals = {p["name"]: list(p["value"].values())[0] for p in parameters}
            for p in parameters:
                if "isNull" in p["value"]:
                    vals[p["name"]] = None
            bound = _BIND.sub(lambda m: _lit(vals[m.group(1)]), sql)
        stripped = bound.strip().lstrip("/*").split("*/")[-1].strip()
        if not stripped.upper().startswith("SELECT") and not stripped.upper().startswith("WITH"):
            self.s.raw(bound)
            return {"records": []}
        rows = self.s.raw(bound)
        return {"records": [[({"isNull": True} if c == "" else {"stringValue": c})
                             for c in row] for row in rows]}


def _cache_execute(api):
    def cache_execute(sql, params):
        sql_params = []
        for k, v in (params or {}).items():
            if v is None:
                sql_params.append({"name": k, "value": {"isNull": True}})
            elif isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
        return api.execute_statement(sql=sql, parameters=sql_params)
    return cache_execute


# ===========================================================================
# The migration itself has to apply, on a real server
# ===========================================================================


def test_migration_applies_and_table_matches_reader_contract(pg):
    _migrate(pg)
    cols = {r[0]: r[1] for r in pg.raw(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'schema_snapshots' ORDER BY column_name")}
    assert cols == {
        "cluster_id": "text",
        "diff_from_previous_json": "jsonb",
        "schema_name": "text",
        "snapshot_time": "timestamp with time zone",
        "tables_json": "jsonb",
        # v27. read_scope is what makes an ABSENCE interpretable at all;
        # last_seen_at is what separates "unchanged for months" from "not seen for
        # months", which is the pair four passes kept resolving to "dropped".
        "read_scope": "text",
        "last_seen_at": "timestamp with time zone",
    }
    # The collector's ON CONFLICT target and the readers' lookup key.
    idx = pg.raw("SELECT indexdef FROM pg_indexes WHERE tablename = 'schema_snapshots'")
    defs = " ".join(r[0] for r in idx)
    assert "UNIQUE" in defs and "cluster_id, schema_name, snapshot_time" in defs
    assert "brin" in defs  # purge support
    # Re-applying must be a no-op: the migrator re-runs every file every deploy.
    _migrate(pg)


# ===========================================================================
# Collector: real catalog, real INSERT, store-on-change
# ===========================================================================


def test_collector_sql_runs_on_real_catalog_and_stores_baseline(pg):
    _migrate(pg)
    pg.raw("DELETE FROM schema_snapshots")
    pg.raw("DROP SCHEMA IF EXISTS app CASCADE; CREATE SCHEMA app")
    pg.raw("CREATE TABLE app.users (id int, email text, name text)")
    pg.raw("CREATE TABLE app.orders (id int, amount numeric)")
    pg.raw("CREATE VIEW app.v_users AS SELECT id FROM app.users")

    api = _DataApi(pg)
    _meta(pg, "prod-pg-1")
    out = collect_pg_schema_snapshot(
        api, _cache_execute(api), "arn:x", "arn:y", "prod-pg-1", "postgres",
        snapshot_ts="2026-07-01T00:00:00+00:00")

    assert out["baselines"] >= 1
    assert out["snapshots_written"] >= 1
    row = pg.execute(
        "SELECT schema_name, tables_json::text AS t, "
        "diff_from_previous_json IS NULL AS diff_null FROM schema_snapshots "
        "WHERE cluster_id = :c AND schema_name = 'app'", {"c": "prod-pg-1"}).rows[0]
    tables = json.loads(row["t"])
    # Real catalog: the VIEW must not be in there, columns must be complete.
    assert set(tables) == {"users", "orders"}
    assert tables["users"] == ["email", "id", "name"]
    # A baseline is NOT a diff. Inventing one would report every existing table
    # as newly ADDED.
    assert row["diff_null"] is True


def test_collector_is_store_on_change(pg):
    """Second run with an unchanged schema must write nothing: 288 runs/day of
    identical rows is what makes the implicit latest-vs-previous diff always
    compare two identical snapshots and answer 'no changes'."""
    api = _DataApi(pg)
    out = collect_pg_schema_snapshot(
        api, _cache_execute(api), "arn:x", "arn:y", "prod-pg-1", "postgres",
        snapshot_ts="2026-07-01T00:05:00+00:00")
    assert out["snapshots_written"] == 0
    assert out["unchanged"] >= 1


def test_collector_second_snapshot_carries_real_diff(pg):
    """Real DDL: add a table, drop a table, alter a table. The stored diff must
    be in compute_diff's bucket shape so schema_history and schema_diff cannot
    describe the same event differently."""
    pg.raw("CREATE TABLE app.audit_log (id int, ts timestamptz)")
    pg.raw("DROP TABLE app.orders")
    pg.raw("ALTER TABLE app.users ADD COLUMN phone text")

    api = _DataApi(pg)
    out = collect_pg_schema_snapshot(
        api, _cache_execute(api), "arn:x", "arn:y", "prod-pg-1", "postgres",
        snapshot_ts="2026-07-01T00:10:00+00:00")
    assert out["snapshots_written"] >= 1

    stored = pg.execute(
        "SELECT diff_from_previous_json::text AS d FROM schema_snapshots "
        "WHERE cluster_id = :c AND schema_name = 'app' "
        "ORDER BY snapshot_time DESC LIMIT 1", {"c": "prod-pg-1"}).rows[0]
    diff = json.loads(stored["d"])
    assert diff["added"] == ["audit_log"]
    assert diff["dropped"] == ["orders"]
    assert diff["modified"] == [
        {"table": "users", "added_columns": ["phone"], "dropped_columns": []}
    ]


# ===========================================================================
# Readers: their REAL SQL, on the rows the REAL collector wrote
# ===========================================================================


def _confirmed_just_now(pg, cluster_id):
    """Stamp this cluster's rows as observed NOW.

    The fixtures write snapshot_ts values in the past, so the collector's own
    last_seen_at is hours old and the readers correctly report every schema as not
    seen recently. Confirmation is measured PER SCHEMA against an ABSOLUTE bar
    (schema_diff_util.CONFIRM_WITHIN_SEC), so a test that wants the CONFIRMED path
    has to say so explicitly rather than have the bar decided by fixture dates.
    """
    pg.raw(f"UPDATE schema_snapshots SET last_seen_at = NOW() "
           f"WHERE cluster_id = '{cluster_id}'")


def test_reader_sql_executes_and_finds_the_real_change(pg):
    """schema_diff's implicit LEFT JOIN + schema_history's window filter +
    diagnose_root_cause's window filter, all as shipped."""
    # This test is about the STATEMENTS, so the cluster is put on the confirmed path
    # explicitly: the fixture back-dates its collections, which correctly makes every
    # schema unconfirmed and would otherwise mix that state into every assertion.
    _confirmed_just_now(pg, "prod-pg-1")
    diff = sd.get_schema_diff_impl(pg, cluster_id="prod-pg-1")
    assert diff["status"] == "ok"
    assert diff["schemas_compared"] == 1
    d = diff["diffs"][0]
    assert d["schema_name"] == "app"
    assert d["added"] == ["audit_log"]
    assert d["dropped"] == ["orders"]
    assert d["modified"][0]["added_columns"] == ["phone"]

    # Explicit two-timestamp mode: exact snapshot_time equality + ::timestamptz.
    exact = sd.get_schema_diff_impl(
        pg, cluster_id="prod-pg-1",
        snapshot_a="2026-07-01T00:00:00+00:00",
        snapshot_b="2026-07-01T00:10:00+00:00")
    assert exact["status"] == "ok"
    assert exact["diffs"][0]["dropped"] == ["orders"]

    # schema_history: the `!= '{}'` filter implicit-casts to jsonb, and the
    # baseline row (NULL diff) must be excluded.
    hist = sh.get_schema_history_impl(pg, cluster_id="prod-pg-1", days=36500)
    assert hist["status"] == "ok"
    assert hist["count"] == 1
    assert json.loads(hist["changes"][0]["changes"])["dropped"] == ["orders"]

    # diagnose_root_cause's DDL signal, same table, half-open range.
    examined, skipped = {}, []
    got = drc._collect_schema_changes(
        pg, "prod-pg-1", "2026-07-01T00:09:00+00:00", "2026-07-01T00:11:00+00:00",
        None, 60, examined, skipped)
    assert skipped == []
    assert examined["schema_changes"] == 1
    assert got[0]["category"] == "schema_change"


def test_baseline_only_cluster_is_not_reported_as_no_changes(pg):
    """One snapshot is not a history. Every surface must say so."""
    pg.raw("DROP SCHEMA IF EXISTS solo CASCADE; CREATE SCHEMA solo")
    pg.raw("CREATE TABLE solo.t (id int)")
    api = _DataApi(pg)
    _meta(pg, "baseline-only-1")
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y",
                               "baseline-only-1", "postgres",
                               snapshot_ts="2026-07-02T00:00:00+00:00")

    diff = sd.get_schema_diff_impl(pg, cluster_id="baseline-only-1")
    assert diff["status"] == "insufficient_snapshots"
    assert diff["schemas_compared"] == 0
    assert diff["baseline_only_schemas"]  # names the schema that has only a baseline
    assert diff["collection_coverage"]["snapshots_stored"] >= 1

    hist = sh.get_schema_history_impl(pg, cluster_id="baseline-only-1")
    assert hist["status"] == "baseline_only"

    _confirmed_just_now(pg, "baseline-only-1")
    examined, skipped = {}, []
    drc._collect_schema_changes(pg, "baseline-only-1", "2026-07-02T00:00:00+00:00",
                               "2026-07-03T00:00:00+00:00", None, 60, examined, skipped)
    assert skipped == ["schema_changes"]
    assert "schema_changes" not in examined


def test_zero_snapshots_is_not_reported_as_no_changes(pg):
    """The table EXISTS and is empty for this cluster: the exact state where the
    old readers said 'no schema changes'."""
    # A registered PG cluster the ETL has met (cluster_meta lands on the first run,
    # before the snapshot collector) and has no snapshots for yet. Without the meta
    # row the dialect is UNKNOWN, which is its own cell:
    # test_a_cluster_whose_engine_is_unknown_is_not_reported_as_empty.
    _meta(pg, "never-collected-1")
    diff = sd.get_schema_diff_impl(pg, cluster_id="never-collected-1")
    assert diff["status"] == "not_collected"
    assert diff["collection_coverage"]["snapshots_stored"] == 0
    assert "수집되지 않" in diff["note"]

    hist = sh.get_schema_history_impl(pg, cluster_id="never-collected-1")
    assert hist["status"] == "not_collected"
    assert hist["count"] == 0
    assert hist["collection_coverage"]["snapshots_stored"] == 0

    examined, skipped = {}, []
    drc._collect_schema_changes(pg, "never-collected-1", "2026-07-01T00:00:00+00:00",
                               "2026-07-03T00:00:00+00:00", None, 60, examined, skipped)
    assert skipped == ["schema_changes"]


def test_no_changes_in_window_still_reports_coverage(pg):
    """Two+ snapshots exist but none in the asked-about window. That is a real
    negative ONLY if every schema was also confirmed to still be there; otherwise
    it covers just the part of the cluster the collector could see."""
    # Back-date the stamps again: this test is precisely about the unconfirmed path.
    pg.raw("UPDATE schema_snapshots SET last_seen_at = NOW() - INTERVAL '9 hours' "
           "WHERE cluster_id = 'prod-pg-1'")
    stale = sh.get_schema_history_impl(pg, cluster_id="prod-pg-1", days=1)
    assert stale["status"] == "partial"
    # `not_seen`, not `stale`. A cluster nothing has confirmed lately IS a cluster
    # where every schema is unconfirmed, and saying it that way NAMES the schemas,
    # which the previous cluster-level `stale` could not: it derived confirmation
    # from the cluster-wide MAX(last_seen_at), so with every schema sharing one
    # stamp (which is what the collector writes, one run timestamp per cycle) the
    # unconfirmed list came back EMPTY. Measured pre-fix over a frozen cycle:
    # collector {"not_seen": 2, "not_seen_schemas": ["alpha","public"]} against
    # readers {"status": "fresh", "unconfirmed_schemas": []}.
    assert stale["observation"]["status"] == "not_seen"
    assert stale["observation"]["unconfirmed_schemas"] == ["app", "public"]
    assert "app" in stale["note"]

    _confirmed_just_now(pg, "prod-pg-1")
    hist = sh.get_schema_history_impl(pg, cluster_id="prod-pg-1", days=1)
    assert hist["status"] == "no_changes"
    assert hist["observation"]["status"] == "fresh"
    assert hist["observation"]["unconfirmed_schemas"] == []
    assert hist["collection_coverage"]["snapshots_stored"] >= 2
    assert hist["collection_coverage"]["first_snapshot"]


def test_purge_keeps_the_comparison_pair_per_schema(pg):
    """The retention DELETE must never take the current baseline: it is the only
    thing the next change has to be diffed against. Nor its PREDECESSOR, which is
    FINDING 3 of the eighth pass and is driven end to end in
    test_retention_does_not_split_the_replay_and_recompute_readers below."""
    # Load by path under a unique module name: several assets in this repo have a
    # top-level handler.py and a bare `import handler` picks whichever one another
    # test put on sys.path first.
    spec = importlib.util.spec_from_file_location(
        "_e4_etl_handler", _ROOT / "data-pipeline" / "etl_collector" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    SCHEMA_SNAPSHOTS_PURGE_SQL = mod.SCHEMA_SNAPSHOTS_PURGE_SQL

    # A schema with THREE rows, seeded here rather than relied on: the exempt set is
    # the pair, so a module whose leftover schemas all have <= 2 rows would leave the
    # purge nothing to delete and this test would prove only that it deleted nothing.
    _meta(pg, "pair-purge-1")
    for days in (10, 11, 12):
        pg.raw(
            "INSERT INTO schema_snapshots (cluster_id, snapshot_time, schema_name, "
            "tables_json, read_scope, last_seen_at) VALUES "
            f"('pair-purge-1', NOW() - INTERVAL '{days} days', 'deep', "
            "'{\"t\": [\"id\"]}'::jsonb, 'db/1', NOW())")

    # Age every row past the cutoff, then run the REAL purge statement.
    pg.raw("UPDATE schema_snapshots SET snapshot_time = snapshot_time - INTERVAL '200 days'")
    before = int(pg.execute("SELECT COUNT(*) AS n FROM schema_snapshots", {}).rows[0]["n"])
    pg.raw(SCHEMA_SNAPSHOTS_PURGE_SQL)
    rows = pg.execute(
        "SELECT cluster_id, schema_name, COUNT(*) AS n FROM schema_snapshots "
        "GROUP BY cluster_id, schema_name", {}).rows
    # At most the PAIR survives per (cluster, schema): the current row and the one it
    # was diffed against. A schema with a single row keeps that one.
    assert rows and all(1 <= int(r["n"]) <= 2 for r in rows), rows
    assert before > sum(int(r["n"]) for r in rows), (
        "the pair exemption must still bound history, not keep everything"
    )
    deep = [r for r in rows if r["cluster_id"] == "pair-purge-1"]
    assert deep and int(deep[0]["n"]) == 2, deep


def test_purge_ages_out_a_schema_orphaned_under_an_abandoned_scope(pg):
    """FINDING 3 of the seventh pass, on the real engine.

    A schema that exists ONLY under a scope the collector no longer reads has
    exactly ONE row, so it was always its own MAX(snapshot_time) and the exemption
    kept it forever. `observed()` reads it as unknown_scope while it still holds
    tables, so it sat in `unconfirmed_schemas` permanently: observation_is_complete
    never returned True again and three consumers were pinned to `partial` for the
    life of the cluster, while the stated 90-day retention bound was false for that
    row. MEASURED before the fix on this harness: after aging every row 200 days,
    the orphan survived and the observation stayed not_seen.

    The LIVE schemas' current rows must still be exempt, or the fix would trade a
    permanent blindness for a destroyed baseline.
    """
    spec = importlib.util.spec_from_file_location(
        "_e4_etl_handler_orphan", _ROOT / "data-pipeline" / "etl_collector" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cid = "orphan-purge-1"
    _meta(pg, cid)
    pg.raw(f"DELETE FROM schema_snapshots WHERE cluster_id = '{cid}'")
    # `live` under the scope the collector still reads, `stray` only under the one it
    # abandoned. All aged past the cutoff. THREE `live` rows, because the exemption is
    # the comparison PAIR: with two the purge would have nothing to delete for this
    # schema and the surviving-rows assertion below would prove only the orphan half.
    for schema, scope, days in (("live", "rightdb/1", 400),
                                ("live", "rightdb/1", 300),
                                ("live", "rightdb/1", 200),
                                ("stray", "wrongdb/2", 250)):
        pg.raw(
            "INSERT INTO schema_snapshots (cluster_id, snapshot_time, schema_name, "
            "tables_json, read_scope, last_seen_at) VALUES "
            f"('{cid}', NOW() - INTERVAL '{days} days', '{schema}', "
            f"'{{\"t\": [\"id\"]}}'::jsonb, '{scope}', NOW() - INTERVAL '{days} days')")
    # The established scope is the newest row that HAS one, which is `live`'s.
    est = pg.execute(sd_util().ESTABLISHED_SCOPE_SQL, {"cluster_id": cid}).rows
    assert est and est[0]["read_scope"] == "rightdb/1", est

    pg.raw(mod.SCHEMA_SNAPSHOTS_PURGE_SQL)
    survived = pg.execute(
        "SELECT schema_name, read_scope, "
        "       EXTRACT(DAY FROM NOW() - snapshot_time)::int AS age_days "
        "FROM schema_snapshots WHERE cluster_id = :c "
        "ORDER BY schema_name, snapshot_time DESC", {"c": cid}).rows
    # The orphan is gone; the live schema keeps exactly its comparison PAIR (the
    # 200-day row and the 300-day row it was diffed against), and its 400-day
    # history is bounded.
    assert survived == [
        {"schema_name": "live", "read_scope": "rightdb/1", "age_days": 200},
        {"schema_name": "live", "read_scope": "rightdb/1", "age_days": 300},
    ], survived

    # And the consequence the finding is actually about: the cluster can be
    # `fresh`/complete again once the orphan is gone.
    pg.raw(f"UPDATE schema_snapshots SET last_seen_at = NOW() WHERE cluster_id = '{cid}'")
    obs = sd_util().observed(lambda s, p: pg.execute(s, p).rows, cid)
    assert obs["status"] == "fresh", obs
    assert obs["unconfirmed_schemas"] == [], obs
    assert sd_util().observation_is_complete(obs) is True


def test_retention_does_not_split_the_replay_and_recompute_readers(pg):
    """FINDING 3 of the eighth pass, driven on the real engine.

    The five consumers are two FAMILIES over the same rows. The timeline,
    get_schema_history and diagnose_root_cause REPLAY `diff_from_previous_json`; the
    dashboard panel and get_schema_diff RECOMPUTE a diff from the PAIR of blobs. When
    the exemption was the newest row alone, a schema whose pre-change baseline aged
    past 90 days kept its stored diff and lost its comparison partner, so the two
    families answered the same question two ways over the same window.

    MEASURED before the fix, one schema with its baseline at 100 days and its change
    at 95 days, both aged past the cutoff and the shipped purge run: ONE row survived,
    get_schema_history over a 365-day window reported count 1 with
    `{"added": ["orders"]}` while get_schema_diff reported `baseline_only` and the
    note "baseline 스냅샷만 있어 비교 대상이 없습니다 (diff에는 최소 2개가 필요합니다)".
    """
    _migrate(pg)
    cid = "retention-split-1"
    _meta(pg, cid)
    pg.raw(f"DELETE FROM schema_snapshots WHERE cluster_id = '{cid}'")
    scope = "rightdb/9"
    for days, tables, diff in (
        (100, {"users": ["id"]}, None),
        (95, {"users": ["id"], "orders": ["id"]}, {"added": ["orders"], "dropped": [],
                                                   "modified": [],
                                                   "rename_candidates": []}),
    ):
        pg.raw(
            "INSERT INTO schema_snapshots (cluster_id, snapshot_time, schema_name, "
            "tables_json, diff_from_previous_json, read_scope, last_seen_at) VALUES "
            f"('{cid}', NOW() - INTERVAL '{days} days', 'app', "
            f"'{json.dumps(tables)}'::jsonb, "
            + ("NULL" if diff is None else f"'{json.dumps(diff)}'::jsonb")
            + f", '{scope}', NOW())")

    spec = importlib.util.spec_from_file_location(
        "_e4_etl_handler_pair", _ROOT / "data-pipeline" / "etl_collector" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pg.raw(mod.SCHEMA_SNAPSHOTS_PURGE_SQL)

    # BOTH rows survive: the change and the baseline it was diffed against.
    assert int(pg.execute(
        "SELECT COUNT(*) AS n FROM schema_snapshots WHERE cluster_id = :c",
        {"c": cid}).rows[0]["n"]) == 2

    # REPLAY family: the event is in the record.
    hist = sh.get_schema_history_impl(pg, cluster_id=cid, days=365)
    assert hist["status"] == "ok", hist
    assert hist["count"] == 1, hist

    # RECOMPUTE family: the same event, from the pair, and NOT `baseline_only`.
    diff = sd.get_schema_diff_impl(pg, cluster_id=cid)
    assert diff["status"] == "ok", diff
    assert diff["totals"]["added"] == 1, diff
    assert diff["diffs"][0]["added"] == ["orders"], diff["diffs"]


def sd_util():
    """The contract module, loaded once by path (api/ and the collectors each ship
    their own verbatim copy; the canonical one is under mcp_servers.shared)."""
    from mcp_servers.shared import schema_diff_util
    return schema_diff_util


def test_a_mysql_cluster_is_refused_by_every_reader_and_not_reported_as_empty(pg):
    """FINDING 4, at the two MCP readers, on the real engine.

    A MySQL cluster has no snapshots because the collector refuses to make any, and
    `not_collected` would then promise a baseline on the next ETL cycle that is never
    coming: an empty success. The refusal has to be the ANSWER.

    THE PRE-REFUSAL ROW IS PRESENT. This test used to DELETE this cluster's rows
    before driving anything, which made the "get_schema_history keeps its rows and
    labels them" half of the decision untested: nothing was there to keep. A real
    MySQL cluster collected before the refusal HAS rows, and they are what the two
    REPLAY readers have to agree about (the other one, api/dashboard `_timeline`, is
    driven on its own harness in
    tests/unit/api/test_dashboard_schema_changes_real_pg.py).
    """
    _migrate(pg)
    cid = "mysql-refused-1"
    _meta(pg, cid, engine="aurora-mysql")
    pg.raw(f"DELETE FROM schema_snapshots WHERE cluster_id = '{cid}'")
    for days, tables, diff_json in (
        (40, {"users": ["id"], "orders": ["id"]}, None),
        (39, {"users": ["id"]}, '{"added": [], "dropped": ["orders"], '
                                '"modified": [], "rename_candidates": []}'),
    ):
        pg.raw(
            "INSERT INTO schema_snapshots (cluster_id, snapshot_time, schema_name, "
            "tables_json, diff_from_previous_json, read_scope, last_seen_at) VALUES "
            f"('{cid}', NOW() - INTERVAL '{days} days', 'appdb', "
            f"'{json.dumps(tables)}'::jsonb, "
            + ("NULL" if diff_json is None else f"'{diff_json}'::jsonb")
            + ", 'collector@localhost', NOW())")

    diff = sd.get_schema_diff_impl(pg, cluster_id=cid)
    assert diff["status"] == "not_supported", diff
    assert diff["observation"]["status"] == "unsupported_engine", diff["observation"]
    assert "PostgreSQL" in diff["note"], diff["note"]
    assert "다음 ETL" not in diff["note"], "it promises a baseline that is not coming"
    # The reader that makes a CLAIM selects no pair at all, however many rows exist.
    assert diff.get("diffs") in ([], None), diff

    hist = sh.get_schema_history_impl(pg, cluster_id=cid, days=36500)
    assert hist["status"] == "not_supported", hist
    assert "PostgreSQL" in hist["note"]
    assert "최초 baseline 스냅샷이 기록됩니다" not in hist["note"]
    # THE RECORD IS KEPT AND LABELLED. Deleting real history is what this surface's
    # contract forbids, so the rows ride along under `not_supported` with a sentence
    # saying they are history and not a current judgment.
    assert hist["count"] == 1, hist
    assert "현재 상태에 대한 판정이 아닙니다" in hist["note"], hist["note"]

    examined, skipped = {}, []
    got = drc._collect_schema_changes(pg, cid, "2026-07-29T00:00:00+00:00",
                                      "2026-07-29T01:00:00+00:00", None, 60,
                                      examined, skipped)
    assert got == []
    assert skipped == ["schema_changes_unsupported_engine"], skipped
    # and an rds_instance MySQL cluster reaches the same place: the family differs,
    # the DIALECT is what decides.
    _meta(pg, cid, engine="mysql")
    assert sd.get_schema_diff_impl(pg, cluster_id=cid)["status"] == "not_supported"


def test_a_cluster_whose_engine_is_unknown_is_not_reported_as_empty_either(pg):
    """FAIL-CLOSED, and a THIRD state rather than either answer. A cluster with no
    cluster_meta row yet (registered seconds ago) is not "unsupported": we cannot
    decide the dialect, so nothing may be claimed and the sentence says which two
    causes to check."""
    _migrate(pg)
    cid = "no-meta-1"
    pg.raw(f"DELETE FROM cluster_meta WHERE cluster_id = '{cid}'")
    pg.raw(f"DELETE FROM schema_snapshots WHERE cluster_id = '{cid}'")
    obs = sd_util().observed(lambda s, p: pg.execute(s, p).rows, cid)
    assert obs["status"] == "unavailable", obs
    assert sd_util().observation_is_complete(obs) is False
    note = sd_util().not_seen_note(obs)
    assert "cluster_meta" in note and "schema_v27" in note, note
    assert sd_util().UNSUPPORTED_DIALECT_NOTE not in note


# ===========================================================================
# A schema that goes to ZERO tables, and a schema that disappears entirely.
# Store-on-change means the last blob written stands as `latest` until something
# replaces it, so a schema the collector stops SEEING keeps serving its dropped
# tables as existing, forever, on all three readers.
# ===========================================================================


def _run_collector(pg, cluster_id, ts):
    api = _DataApi(pg)
    _meta(pg, cluster_id)  # the ETL writes cluster_meta before the snapshot collector
    return collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y",
                                      cluster_id, "postgres", snapshot_ts=ts)


def _latest(pg, cluster_id, schema_name):
    return pg.execute(
        "SELECT tables_json::text AS t, diff_from_previous_json::text AS d, "
        "       read_scope, last_seen_at::text AS seen "
        "FROM schema_snapshots WHERE cluster_id = :c AND schema_name = :s "
        "ORDER BY snapshot_time DESC LIMIT 1",
        {"c": cluster_id, "s": schema_name}).rows[0]


def test_dropping_the_last_table_is_seen_by_all_three_readers(pg):
    """Measured before the fix: the second run reported
    {"schemas_seen": 3, "snapshots_written": 0, "baselines": 0, "unchanged": 3}
    and the stored row still listed only_table. PG_SCHEMA_SQL now drives off
    pg_namespace, so an emptied schema returns a row carrying '{}' and the drop is
    OBSERVED rather than inferred from absence."""
    pg.raw("DROP SCHEMA IF EXISTS zeroed CASCADE; CREATE SCHEMA zeroed")
    pg.raw("CREATE TABLE zeroed.only_table (id int, payload text)")
    assert _run_collector(pg, "zero-1", "2026-07-20T00:00:00+00:00")["baselines"] >= 1
    assert "only_table" in _latest(pg, "zero-1", "zeroed")["t"]

    pg.raw("DROP TABLE zeroed.only_table")
    out = _run_collector(pg, "zero-1", "2026-07-20T00:05:00+00:00")
    assert out["snapshots_written"] == 1
    assert out["emptied"] == 1   # DIRECTLY observed: named here, holds nothing
    assert out["not_seen"] == 0  # nothing was inferred from an absence
    row = _latest(pg, "zero-1", "zeroed")
    assert json.loads(row["t"]) == {}
    assert json.loads(row["d"])["dropped"] == ["only_table"]

    # All three readers, on the rows the real collector wrote.
    diff = sd.get_schema_diff_impl(pg, cluster_id="zero-1")
    zeroed = [d for d in diff["diffs"] if d["schema_name"] == "zeroed"]
    assert zeroed and zeroed[0]["dropped"] == ["only_table"]
    hist = sh.get_schema_history_impl(pg, cluster_id="zero-1", days=36500)
    assert any(json.loads(c["changes"]).get("dropped") == ["only_table"]
               for c in hist["changes"])
    _confirmed_just_now(pg, "zero-1")
    examined, skipped = {}, []
    got = drc._collect_schema_changes(pg, "zero-1", "2026-07-20T00:04:00+00:00",
                                      "2026-07-20T00:06:00+00:00", None, 60,
                                      examined, skipped)
    assert skipped == []
    assert examined["schema_changes"] == 1
    assert got[0]["evidence"]["schema_name"] == "zeroed"

    # 288 runs a day: an already-empty schema must not be re-recorded.
    assert _run_collector(pg, "zero-1", "2026-07-20T00:10:00+00:00")["snapshots_written"] == 0


def test_a_dropped_schema_is_reported_unknown_and_never_as_a_drop(pg):
    """DROP SCHEMA leaves no row to iterate at all. So does a read that could not
    reach the schema, and nothing inside the read separates the two, so the drop
    is NOT recorded. THIS TEST STATES THE COST: a real DROP SCHEMA stops being
    reported as a drop and becomes an explicit unknown instead.

    Before this pass the same scenario wrote tables_json '{}' with a dropped diff,
    which is why a read against the wrong database reported a mass DROP."""
    pg.raw("DROP DATABASE IF EXISTS wipedb", db="postgres")
    pg.raw("CREATE DATABASE wipedb", db="postgres")
    pg.raw("CREATE SCHEMA keep; CREATE TABLE keep.t1 (id int)", db="wipedb")
    pg.raw("CREATE SCHEMA wiped; CREATE TABLE wiped.t1 (id int);"
           "CREATE TABLE wiped.t2 (id int)", db="wipedb")
    assert _run_in(pg, "wipedb", "wipe-1", "2026-07-21T00:00:00+00:00")["baselines"] == 3
    # A real change on ANOTHER schema, so the cluster has comparable history and
    # the reader statuses below are about the unknown rather than about coverage.
    pg.raw("CREATE TABLE keep.t2 (id int)", db="wipedb")
    assert _run_in(pg, "wipedb", "wipe-1", "2026-07-21T00:05:00+00:00")["changes"] == 1
    stored = _latest(pg, "wipe-1", "wiped")

    pg.raw("DROP SCHEMA wiped CASCADE", db="wipedb")
    # The CURRENT cycle, stamped NOW: confirmation is per schema against an absolute
    # bar, so a back-dated final run would leave `keep` unconfirmed too and this test
    # could not isolate the schema that actually vanished.
    out = _run_now(pg, "wipedb", "wipe-1")
    assert out["not_seen"] == 1
    assert out["not_seen_schemas"] == ["wiped"]
    assert out["snapshots_written"] == 0
    # Nothing was written, so what is stored is still the last thing observed.
    assert _latest(pg, "wipe-1", "wiped") == stored

    # The READERS are where the unknown has to show up, or it is not a state, it
    # is a docstring.
    diff = sd.get_schema_diff_impl(pg, cluster_id="wipe-1")
    assert diff["observation"]["unconfirmed_schemas"] == ["wiped"]
    assert "wiped" in diff["note"] and "확인 불가" in diff["note"]
    assert diff["totals"]["dropped"] == 0
    hist = sh.get_schema_history_impl(pg, cluster_id="wipe-1", days=36500)
    assert hist["status"] == "ok"  # keep's real change is still reported
    assert "wiped" in hist["note"] and "확인 불가" in hist["note"]
    # An EMPTY window over the same cluster: never "no_changes" while a schema is
    # unaccounted for.
    empty_window = sh.get_schema_history_impl(pg, cluster_id="wipe-1", days=1)
    assert empty_window["count"] == 0
    assert empty_window["status"] == "partial"
    assert "wiped" in empty_window["note"]
    examined, skipped = {}, []
    got = drc._collect_schema_changes(pg, "wipe-1", "2026-07-21T00:09:00+00:00",
                                      "2026-07-21T00:11:00+00:00", None, 60,
                                      examined, skipped)
    assert got == []  # and no DDL signal is manufactured for the RCA either
    # ...but the RCA is TOLD, or an empty highest-weight source reads as "we looked
    # and there was no DDL change" over a cluster with a schema nobody can see.
    assert skipped == ["schema_changes_unconfirmed_schemas"]

    # Repeating for 288 runs a day changes nothing and files nothing.
    again = _run_now(pg, "wipedb", "wipe-1")
    assert again["snapshots_written"] == 0 and again["not_seen"] == 1


def test_a_catalog_read_that_returned_nothing_never_invents_a_mass_drop(pg):
    """Zero rows means the read reported no scope either, so nothing in it can be
    compared with anything stored."""
    pg.raw("DROP SCHEMA IF EXISTS blind CASCADE; CREATE SCHEMA blind")
    pg.raw("CREATE TABLE blind.keeper (id int)")
    _run_collector(pg, "blind-1", "2026-07-22T00:00:00+00:00")
    before = _latest(pg, "blind-1", "blind")["t"]

    class _NoSchemas:
        def execute_statement(self, **kw):
            return {"records": []}

    api = _DataApi(pg)
    out = collect_pg_schema_snapshot(_NoSchemas(), _cache_execute(api), "arn:x", "arn:y",
                                     "blind-1", "postgres",
                                     snapshot_ts="2026-07-22T00:05:00+00:00")
    assert out["scope_status"] == "scope_unknown"
    assert out["read_scope"] == ""
    assert out["snapshots_written"] == 0
    assert _latest(pg, "blind-1", "blind")["t"] == before


# ===========================================================================
# The RCA producer probe, EXECUTED. It was the one new statement in this tier
# that no test ran: a column rename inside it left the suite green at 89 passed.
# ===========================================================================


def test_rca_producer_probe_executes_and_a_broken_probe_is_labelled_apart(pg):
    rows = pg.execute(drc.SCHEMA_PRODUCER_PROBE_SQL, {"cluster_id": "zero-1"}).rows
    assert rows and {"snapshots", "schemas"} <= set(rows[0])
    assert int(rows[0]["snapshots"]) > 0

    # A window with no rows so the probe actually runs, on a baseline-only cluster.
    pg.raw("DROP SCHEMA IF EXISTS lonely CASCADE; CREATE SCHEMA lonely")
    pg.raw("CREATE TABLE lonely.t (id int)")
    _run_collector(pg, "probe-1", "2026-07-23T00:00:00+00:00")
    _confirmed_just_now(pg, "probe-1")  # this test is about the PROBE, not the clock
    examined, skipped = {}, []
    drc._collect_schema_changes(pg, "probe-1", "2026-07-23T01:00:00+00:00",
                                "2026-07-23T02:00:00+00:00", None, 60,
                                examined, skipped)
    assert skipped == ["schema_changes"]

    class _BrokenProbe:
        """What a column typo in the probe does live: the window query is fine and
        the probe raises."""

        def execute(self, sql, params=None):
            if "COUNT(DISTINCT schema_name)" in sql:
                return pg.execute(sql.replace("schema_name)", "schema_nameX)"), params)
            return pg.execute(sql, params)

    broken_examined, broken_skipped = {}, []
    drc._collect_schema_changes(_BrokenProbe(), "probe-1", "2026-07-23T01:00:00+00:00",
                                "2026-07-23T02:00:00+00:00", None, 60,
                                broken_examined, broken_skipped)
    assert broken_skipped == ["schema_changes_probe_error"]
    assert broken_skipped != skipped, (
        "a broken probe must not be indistinguishable from a cluster that simply "
        "has no comparable history"
    )


# ===========================================================================
# get_schema_diff: the payload has to say WHEN, and the argument order must not
# decide WHICH WAY the diff runs.
# ===========================================================================


def test_explicit_diff_runs_the_same_way_round_either_way(pg):
    """Measured before the fix: the same cluster answered added 2 / dropped 0 or
    dropped 2 / added 0 depending purely on argument order, status ok both times,
    so a CREATE was reported to a DBA as a DROP."""
    pg.raw("DROP SCHEMA IF EXISTS dated CASCADE; CREATE SCHEMA dated")
    # Different column signatures on purpose: two single-`id` tables are a
    # rename_candidate pair, which is the correct answer to a different question.
    pg.raw("CREATE TABLE dated.customers (id int, email text)")
    _run_collector(pg, "dated-1", "2026-07-01T00:00:00+00:00")
    pg.raw("CREATE TABLE dated.invoices (id int)")
    pg.raw("DROP TABLE dated.customers")
    _run_collector(pg, "dated-1", "2026-07-18T23:50:46+00:00")

    def _dated(result):
        return [d for d in result["diffs"] if d["schema_name"] == "dated"][0]

    forward = _dated(sd.get_schema_diff_impl(
        pg, cluster_id="dated-1",
        snapshot_a="2026-07-01T00:00:00+00:00",
        snapshot_b="2026-07-18T23:50:46+00:00"))
    reversed_ = _dated(sd.get_schema_diff_impl(
        pg, cluster_id="dated-1",
        snapshot_a="2026-07-18T23:50:46+00:00",
        snapshot_b="2026-07-01T00:00:00+00:00"))
    assert forward["added"] == ["invoices"]
    assert forward["dropped"] == ["customers"]
    assert forward == reversed_


def test_successful_diff_is_dated_on_the_real_engine(pg):
    """The success payload used to carry no timestamp of any kind, while the
    producer is store-on-change: the implicit diff is the newest DDL EVENT
    whenever it happened, and the agent presented it as recent."""
    out = sd.get_schema_diff_impl(pg, cluster_id="dated-1")
    dated = [d for d in out["diffs"] if d["schema_name"] == "dated"][0]
    # The '+09'-rendered server text of 2026-07-18T23:50:46Z.
    assert dated["snapshot_time"].startswith("2026-07-1")
    assert dated["previous_snapshot_time"].startswith("2026-07-0")
    assert out["collection_coverage"]["last_snapshot"]
    assert dated["snapshot_time"] in out["note"]


# ===========================================================================
# ROUND 4: what "the read did not see it" is allowed to mean.
#
# The previous guard was `if absent and not returned`, and on PostgreSQL it could
# never fire: pg_namespace returns `public` for EVERY database, so a successful
# read is never empty. These tests establish what the shipped statement actually
# guarantees, then drive the replacement guard until it fires.
# ===========================================================================


class _DataApiIn(_DataApi):
    """Same adapter, but the TARGET read runs in a chosen database while the cache
    reads and writes stay in `postgres`, which is the real split: the target is a
    customer cluster, the cache is ours. The collector's target call is the only
    one that passes `database`."""

    def __init__(self, server, target_db):
        super().__init__(server)
        self.target_db = target_db

    def execute_statement(self, resourceArn=None, secretArn=None, database=None,
                          sql=None, parameters=None, includeResultMetadata=None):
        if database is None:
            return super().execute_statement(sql=sql, parameters=parameters)
        rows = self.s.raw(f"/* target */ {sql}", db=self.target_db)
        return {"records": [[({"isNull": True} if c == "" else {"stringValue": c})
                             for c in row] for row in rows]}


def _run_in(pg, target_db, cluster_id, ts):
    api = _DataApiIn(pg, target_db)
    _meta(pg, cluster_id)  # the ETL writes cluster_meta before the snapshot collector
    return collect_pg_schema_snapshot(api, _cache_execute(_DataApi(pg)), "arn:x",
                                      "arn:y", cluster_id, target_db, snapshot_ts=ts)


def _run_now(pg, target_db, cluster_id):
    """The CURRENT cycle: snapshot_ts=None, so the shipped code stamps NOW().

    Needed because confirmation is measured PER SCHEMA against an ABSOLUTE bar
    (schema_diff_util.CONFIRM_WITHIN_SEC, 15 minutes) rather than against the
    cluster-wide MAX(last_seen_at). A scenario whose LAST collection is back-dated
    has, correctly, confirmed nothing recently, so EVERY schema comes back
    unconfirmed and a test meaning to isolate ONE vanished schema cannot see it.
    Back-dating the earlier runs is still right: they build the store-on-change
    history."""
    return _run_in(pg, target_db, cluster_id, None)


def test_the_pg_catalog_read_does_not_shrink_when_privileges_are_lost(pg):
    """One of the three hazards the previous tier named as making absence
    ambiguous. On PostgreSQL it is NOT one: pg_catalog is world-readable, so an
    unprivileged role gets the identical result set while being unable to read a
    single row of the tables it just listed. The MySQL half of this claim is the
    opposite and stays disclosed, information_schema there IS privilege-filtered.
    """
    pg.raw("DROP DATABASE IF EXISTS privdb", db="postgres")
    pg.raw("CREATE DATABASE privdb", db="postgres")
    pg.raw("DROP ROLE IF EXISTS lowpriv", db="postgres")
    pg.raw("CREATE ROLE lowpriv LOGIN", db="postgres")
    pg.raw("CREATE SCHEMA core; CREATE TABLE core.users (id int, email text);"
           "CREATE TABLE core.orders (id int)", db="privdb")
    pg.raw("CREATE TABLE public.audit (id int)", db="privdb")
    pg.raw("REVOKE ALL ON SCHEMA core FROM PUBLIC;"
           "REVOKE ALL ON ALL TABLES IN SCHEMA core FROM PUBLIC", db="privdb")

    as_super = pg.raw(PG_SCHEMA_SQL, db="privdb")
    as_low = pg.raw(PG_SCHEMA_SQL, db="privdb", user="lowpriv")
    assert [(r[0], r[1]) for r in as_super] == [("core", "2"), ("public", "1")]
    assert as_low == as_super, "PG catalog visibility must not depend on grants"
    with pytest.raises(AssertionError, match="permission denied"):
        pg.raw("SELECT count(*) FROM core.users", db="privdb", user="lowpriv")


def test_the_read_returns_the_complete_visible_set_or_raises(pg):
    """The other half of the claim the guard rests on. Compared against an
    INDEPENDENT catalog census rather than against itself, and the failure half is
    driven with a real server FATAL."""
    census = {r[0] for r in pg.raw(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT IN "
        "('pg_catalog', 'information_schema') AND nspname NOT LIKE 'pg\\_%'",
        db="privdb")}
    assert {r[0] for r in pg.raw(PG_SCHEMA_SQL, db="privdb")} == census == {"core", "public"}

    before = int(pg.execute("SELECT COUNT(*) AS n FROM schema_snapshots", {}).rows[0]["n"])
    with pytest.raises(AssertionError, match='database "nosuchdb" does not exist'):
        _run_in(pg, "nosuchdb", "scope-1", "2026-07-24T00:00:00+00:00")
    after = int(pg.execute("SELECT COUNT(*) AS n FROM schema_snapshots", {}).rows[0]["n"])
    assert after == before, "a failed read must write nothing at all"


def test_a_read_that_landed_in_the_wrong_database_records_no_drop(pg):
    """THE CRITICAL, fifth pass, end to end. `target_db = resource.get("db_name",
    "sampledb")` in etl_collector/handler.py makes this a live production path.

    The wrong database's `public` HOLDS A TABLE here, which is what `public`
    normally does on PostgreSQL, and that is exactly what the pass-4 corroboration
    predicate could not survive. MEASURED on this fixture against the pass-4 code:
      {"corroborated": true, "schemas_seen": 1, "snapshots_written": 3,
       "vanished": 2, "vanished_unconfirmed": 0}
      get_schema_diff      status ok, totals dropped 4
                           core [orders, users], billing [invoices], public [audit]
      get_schema_history   count 3
      diagnose_root_cause  3 DDL signals examined, skipped []
    Note that `public`'s row was an ORDINARY diff (dropped [audit], added
    [app_settings]), not the absence inference, so no predicate on absence could
    have covered it. What covers both halves is comparability: the read says which
    catalog it reached, and that is not the one the history came from.
    """
    pg.raw("DROP DATABASE IF EXISTS rightdb", db="postgres")
    pg.raw("DROP DATABASE IF EXISTS sampledb", db="postgres")
    pg.raw("CREATE DATABASE rightdb", db="postgres")
    pg.raw("CREATE DATABASE sampledb", db="postgres")  # the db_name fallback
    pg.raw("CREATE SCHEMA core; CREATE TABLE core.users (id int, email text);"
           "CREATE TABLE core.orders (id int, total numeric)", db="rightdb")
    pg.raw("CREATE SCHEMA billing; CREATE TABLE billing.invoices (id int)", db="rightdb")
    pg.raw("CREATE TABLE public.audit (id int, note text)", db="rightdb")
    pg.raw("CREATE TABLE public.app_settings (k text, v text)", db="sampledb")

    base = _run_in(pg, "rightdb", "wrongdb-1", "2026-07-25T00:00:00+00:00")
    assert base["baselines"] == 3
    assert base["scope_status"] == "adopted"
    assert base["read_scope"].startswith("rightdb/")
    stored = {s: _latest(pg, "wrongdb-1", s) for s in ("core", "billing", "public")}
    rows_before = int(pg.execute(
        "SELECT COUNT(*) AS n FROM schema_snapshots WHERE cluster_id = 'wrongdb-1'",
        {}).rows[0]["n"])

    # `public` exists in EVERY PostgreSQL database and here it holds a table, so
    # the wrong database's read is neither empty nor uncorroborated.
    assert [(r[0], r[1]) for r in pg.raw(PG_SCHEMA_SQL, db="sampledb")] == [("public", "1")]

    out = _run_in(pg, "sampledb", "wrongdb-1", "2026-07-25T00:05:00+00:00")
    assert out["scope_status"] == "rescoped"
    assert out["read_scope"].startswith("sampledb/")
    # THE PASS-5 FREEZE IS GONE and this is what replaces it. The freeze wrote
    # nothing at all for one stated reason: the dashboard panel recomputed its own
    # base-vs-latest blob diff with no notion of scope. That reader is now
    # scope-filtered, and the freeze had a measured cost: this very cluster stayed on
    # snapshots_written 0 FOREVER afterwards, so the phantom drop the readers were
    # already reporting could not heal even after the operator fixed the config.
    #
    # What must hold instead is that NOTHING IS DIFFED across the two scopes.
    assert out["changes"] == 0
    assert out["baselines"] == 1  # `public`, the one schema this read saw
    assert out["not_seen_schemas"] == ["billing", "core"]
    # The other scope's rows are untouched: not deleted, not overwritten, not
    # re-diffed. They are simply not comparable to this read.
    assert {s: _latest(pg, "wrongdb-1", s) for s in ("core", "billing")} == \
        {s: stored[s] for s in ("core", "billing")}
    # `public`'s new row is a BASELINE, NULL diff, under the new scope. A diff here
    # would be the phantom drop.
    fresh_public = _latest(pg, "wrongdb-1", "public")
    assert fresh_public["d"] is None
    assert fresh_public["read_scope"].startswith("sampledb/")

    # END TO END: what the three readers hand a human is the last GOOD snapshot,
    # and not one word about a drop.
    diff = sd.get_schema_diff_impl(pg, cluster_id="wrongdb-1")
    assert diff["totals"]["dropped"] == 0
    assert all(not d["dropped"] for d in diff.get("diffs", []))
    # `not_seen`, and it NAMES the schemas the read could not reach, which the
    # previous cluster-level `stale` could not.
    assert diff["observation"]["status"] == "not_seen"
    assert "core" in diff["observation"]["unconfirmed_schemas"]
    hist = sh.get_schema_history_impl(pg, cluster_id="wrongdb-1", days=36500)
    assert hist["count"] == 0
    examined, skipped = {}, []
    got = drc._collect_schema_changes(pg, "wrongdb-1", "2026-07-25T00:04:00+00:00",
                                      "2026-07-25T00:06:00+00:00", None, 60,
                                      examined, skipped)
    assert got == []
    # BOTH labels: this cluster has no comparable history under the new scope AND it
    # has schemas nobody can currently confirm. They are separate facts with separate
    # operator actions, so they carry separate labels.
    # ONE label: the probe found snapshots > schemas, so this cluster DOES have
    # comparable history under some scope. What it does not have is a confirmation
    # for the schemas the bad read could not reach.
    assert skipped == ["schema_changes_unconfirmed_schemas"]

    # SELF-HEALING: the next correct read records the truth, including the real
    # DDL that happened while the collector was looking at the wrong database.
    pg.raw("DROP TABLE billing.invoices", db="rightdb")
    heal = _run_in(pg, "rightdb", "wrongdb-1", "2026-07-25T00:10:00+00:00")
    # Still `rescoped`, because `public` now carries the sampledb scope from the one
    # bad read and part of the history is therefore recorded elsewhere. That is a
    # report, not a freeze, and it is exactly what the pass-5 behaviour could not do:
    # the real DDL below IS picked up on this very cycle.
    assert heal["scope_status"] == "rescoped"
    assert heal["snapshots_written"] == 2  # billing's real change + public re-baselined
    assert heal["emptied"] == 1  # billing still exists in rightdb and holds nothing
    assert json.loads(_latest(pg, "wrongdb-1", "billing")["d"])["dropped"] == ["invoices"]


def test_a_same_named_database_on_another_server_is_not_comparable_either(pg):
    """The database NAME alone would have let this through: drop and recreate
    `rightdb` and it is a different database that happens to share a name, with no
    relation to the stored history. The oid in the scope catches it."""
    pg.raw("DROP DATABASE IF EXISTS twin", db="postgres")
    pg.raw("CREATE DATABASE twin", db="postgres")
    pg.raw("CREATE SCHEMA core; CREATE TABLE core.users (id int)", db="twin")
    first = _run_in(pg, "twin", "twin-1", "2026-07-27T00:00:00+00:00")
    assert first["baselines"] == 2  # core + public

    pg.raw("DROP DATABASE twin", db="postgres")
    pg.raw("CREATE DATABASE twin", db="postgres")  # same name, new oid, no core
    out = _run_in(pg, "twin", "twin-1", "2026-07-27T00:05:00+00:00")
    assert out["scope_status"] == "rescoped"
    assert out["changes"] == 0  # nothing is DIFFED across the two oids
    # `core` does not exist in the new database, so it is not re-baselined and its
    # stored blob is left exactly as it was: unconfirmed, never dropped.
    assert json.loads(_latest(pg, "twin-1", "core")["t"]) == {"users": ["id"]}
    assert out["not_seen_schemas"] == ["core"]


def test_the_only_schema_going_to_zero_is_still_recorded_as_a_drop(pg):
    """The single-schema cluster is the common shape, and it must not go back to
    serving dropped tables forever. It does not have to: the catalog NAMED the
    schema and reported it holds nothing, under a scope that matches the stored
    history. That is a direct observation and it needs no corroboration from
    another schema (which is what the pass-4 predicate asked for, and what the
    wrong database supplied)."""
    pg.raw("DROP DATABASE IF EXISTS solodb", db="postgres")
    pg.raw("CREATE DATABASE solodb", db="postgres")
    pg.raw("CREATE TABLE public.only_table (id int, payload text)", db="solodb")
    assert _run_in(pg, "solodb", "solo-1", "2026-07-26T00:00:00+00:00")["baselines"] == 1

    pg.raw("DROP TABLE public.only_table", db="solodb")
    out = _run_in(pg, "solodb", "solo-1", "2026-07-26T00:05:00+00:00")
    assert out["scope_status"] == "matched"
    assert out["emptied"] == 1
    assert out["snapshots_written"] == 1
    assert out["not_seen"] == 0
    assert json.loads(_latest(pg, "solo-1", "public")["t"]) == {}
    assert json.loads(_latest(pg, "solo-1", "public")["d"])["dropped"] == ["only_table"]

    hist = sh.get_schema_history_impl(pg, cluster_id="solo-1", days=36500)
    assert any(json.loads(c["changes"]).get("dropped") == ["only_table"]
               for c in hist["changes"])
    # The same observation from ANOTHER scope records nothing at all.
    pg.raw("DROP DATABASE IF EXISTS solodb2", db="postgres")
    pg.raw("CREATE DATABASE solodb2", db="postgres")
    elsewhere = _run_in(pg, "solodb2", "solo-1", "2026-07-26T00:07:00+00:00")
    assert elsewhere["scope_status"] == "rescoped"
    assert elsewhere["changes"] == 0  # a baseline, never a diff across scopes
    # Coming BACK writes one baseline per schema, once: the other scope's row is not
    # a comparable predecessor, so there is nothing to diff against and a baseline is
    # the only honest thing to store. It is not re-filed on the 288 runs after that.
    back = _run_in(pg, "solodb", "solo-1", "2026-07-26T00:10:00+00:00")
    assert back["baselines"] == 1 and back["changes"] == 0
    assert _run_in(pg, "solodb", "solo-1", "2026-07-26T00:15:00+00:00")[
        "snapshots_written"] == 0


def test_an_unchanged_run_records_that_the_schema_was_still_there(pg):
    """The heartbeat, on the real engine. Without it snapshot_time is the only
    date on the row, and for a stable schema that is months old, so "unchanged"
    and "not seen" are the same two rows in the table."""
    pg.raw("DROP DATABASE IF EXISTS beatdb", db="postgres")
    pg.raw("CREATE DATABASE beatdb", db="postgres")
    pg.raw("CREATE SCHEMA app; CREATE TABLE app.t (id int)", db="beatdb")
    _run_in(pg, "beatdb", "beat-1", "2026-07-28T00:00:00+00:00")
    first = _latest(pg, "beat-1", "app")
    assert first["seen"] is not None

    out = _run_in(pg, "beatdb", "beat-1", "2026-07-28T06:00:00+00:00")
    assert out["snapshots_written"] == 0 and out["unchanged"] == 2
    later = _latest(pg, "beat-1", "app")
    # Same row (snapshot_time unchanged, store-on-change), newer observation.
    assert later["t"] == first["t"]
    assert later["seen"] > first["seen"]
    assert int(pg.execute(
        "SELECT COUNT(*) AS n FROM schema_snapshots WHERE cluster_id = 'beat-1'",
        {}).rows[0]["n"]) == 2  # app + public, one row each

    # A read from another scope confirms NOTHING, which is what makes a
    # scope-frozen cluster visible to the readers at all.
    pg.raw("DROP DATABASE IF EXISTS beatdb2", db="postgres")
    pg.raw("CREATE DATABASE beatdb2", db="postgres")
    _run_in(pg, "beatdb2", "beat-1", "2026-07-28T12:00:00+00:00")
    assert _latest(pg, "beat-1", "app")["seen"] == later["seen"]


def test_rows_written_before_the_migration_are_re_baselined_once(pg):
    """An existing deployment's rows carry no scope, so they are not comparable to
    anything. They must not freeze the cluster out of collection forever either:
    the first read under a known scope adopts it and re-baselines. A baseline
    carries a NULL diff, so no reader reports it as a DDL change."""
    pg.raw("DROP DATABASE IF EXISTS legacydb", db="postgres")
    pg.raw("CREATE DATABASE legacydb", db="postgres")
    pg.raw("CREATE TABLE public.t1 (id int)", db="legacydb")
    pg.raw("INSERT INTO schema_snapshots (cluster_id, snapshot_time, schema_name, "
           "tables_json) VALUES ('legacy-1', '2026-01-01T00:00:00+00', 'public', "
           """'{"t1": ["id"]}')""")

    out = _run_in(pg, "legacydb", "legacy-1", "2026-07-29T00:00:00+00:00")
    assert out["scope_status"] == "adopted"
    assert out["baselines"] == 1 and out["changes"] == 0
    rows = pg.execute(
        "SELECT read_scope, diff_from_previous_json IS NULL AS baseline, "
        "       last_seen_at IS NULL AS unseen FROM schema_snapshots "
        "WHERE cluster_id = 'legacy-1' ORDER BY snapshot_time", {}).rows
    assert [r["read_scope"] for r in rows] == [None, out["read_scope"]]
    assert all(r["baseline"] is True for r in rows)
    assert [r["unseen"] for r in rows] == [True, False]
    # The re-baseline is not a change: get_schema_history has nothing to report.
    assert sh.get_schema_history_impl(pg, cluster_id="legacy-1", days=36500)["count"] == 0

    # A legacy schema the read does not name is reported as an unknown, not as a
    # drop and not as an unchanged schema.
    pg.raw("INSERT INTO schema_snapshots (cluster_id, snapshot_time, schema_name, "
           "tables_json) VALUES ('legacy-1', '2026-01-01T00:00:00+00', 'ghost', "
           """'{"gone": ["id"]}')""")
    ghosted = _run_in(pg, "legacydb", "legacy-1", "2026-07-29T00:05:00+00:00")
    assert ghosted["not_seen_schemas"] == ["ghost"]
    diff = sd.get_schema_diff_impl(pg, cluster_id="legacy-1")
    assert "ghost" in diff["observation"]["unconfirmed_schemas"]


def test_the_rca_read_failing_is_not_reported_as_a_cluster_with_no_history(pg):
    """FINDING 2 on the real engine: the MAIN schema_snapshots read raising must
    not borrow the label that means "this cluster has no comparable history"."""
    class _NoTable:
        def execute(self, sql, params=None):
            if "FROM schema_snapshots" in sql:
                return pg.execute(sql.replace("schema_snapshots", "schema_snapshots_x"),
                                  params)
            return pg.execute(sql, params)

    examined, skipped = {}, []
    drc._collect_schema_changes(_NoTable(), "solo-1", "2026-07-26T00:00:00+00:00",
                                "2026-07-26T01:00:00+00:00", None, 60, examined, skipped)
    assert skipped == ["schema_changes_read_error"]

    real_examined, real_skipped = {}, []
    drc._collect_schema_changes(pg, "never-collected-1", "2026-07-26T00:00:00+00:00",
                                "2026-07-26T01:00:00+00:00", None, 60,
                                real_examined, real_skipped)
    assert real_skipped == ["schema_changes"]
    assert skipped != real_skipped


def test_explicit_miss_names_the_snapshots_that_do_exist(pg):
    """A baseline-only cluster used to answer "only a baseline exists" to a caller
    who asked about two specific timestamps, dropping the coverage range."""
    miss = sd.get_schema_diff_impl(
        pg, cluster_id="probe-1",
        snapshot_a="2020-01-01T00:00:00+00:00",
        snapshot_b="2020-01-02T00:00:00+00:00")
    assert miss["status"] == "snapshots_not_found"
    assert miss["collection_coverage"]["first_snapshot"] in miss["note"]
    # The implicit call on that same cluster still reports the baseline honestly.
    assert sd.get_schema_diff_impl(
        pg, cluster_id="probe-1")["status"] == "insufficient_snapshots"


def test_the_comparison_partner_is_the_latest_row_or_nothing(pg):
    """A NEWER row without a scope can exist: during a rolling deploy an old
    Lambda version can insert after a new one has. Asking for "the newest row that
    happens to carry my scope" would then compare against a row that is no longer
    the current state. The partner is the LATEST row and only if its scope
    matches, so this re-baselines instead."""
    _migrate(pg)  # idempotent; this test does not depend on an earlier one running
    pg.raw("DROP DATABASE IF EXISTS raced", db="postgres")
    pg.raw("CREATE DATABASE raced", db="postgres")
    pg.raw("CREATE SCHEMA app; CREATE TABLE app.t1 (id int)", db="raced")
    scoped = _run_in(pg, "raced", "race-1", "2026-07-30T00:00:00+00:00")
    assert scoped["baselines"] == 2

    # The old version's write: no scope, and NEWER than the scoped row.
    pg.raw("INSERT INTO schema_snapshots (cluster_id, snapshot_time, schema_name, "
           "tables_json, diff_from_previous_json) VALUES ('race-1', "
           """'2026-07-30T00:02:00+00', 'app', '{"t1": ["id"], "t2": ["id"]}', """
           """'{"added": ["t2"], "dropped": [], "modified": [], "rename_candidates": []}')""")

    out = _run_in(pg, "raced", "race-1", "2026-07-30T00:05:00+00:00")
    assert out["scope_status"] == "matched"
    assert out["baselines"] == 1  # app, re-baselined against nothing comparable
    assert out["changes"] == 0    # NOT a diff against a row that is not the state
    row = _latest(pg, "race-1", "app")
    assert row["d"] is None and json.loads(row["t"]) == {"t1": ["id"]}
    # And the heartbeat lands on the row the readers now call latest.
    assert row["seen"] is not None
    beat = _run_in(pg, "raced", "race-1", "2026-07-30T00:10:00+00:00")
    assert beat["unchanged"] == 2 and beat["snapshots_written"] == 0
    assert _latest(pg, "race-1", "app")["seen"] > row["seen"]


def test_a_real_change_across_one_foreign_cycle_is_a_change_and_not_a_baseline(pg):
    """FINDING 1 of the SEVENTH pass, driven end to end.

    ONE cycle reading another catalog leaves the schema's newest row under that
    other scope. A genuine DDL change landing on the NEXT same-scope cycle then has
    a same-scope predecessor sitting right there, and the collector used to ignore it
    (`prev_scope` came from the CROSS-SCOPE latest row) and store a BASELINE with a
    NULL diff. The event was therefore invisible to the three REPLAY consumers and
    visible to the two RECOMPUTE ones, so the product answered the same question two
    ways.

    MEASURED on PostgreSQL 14.18 before the fix, a real CREATE TABLE after one
    wrong-database cycle:
      collector           {"snapshots_written": 2, "baselines": 2, "changes": 0}
      get_schema_history  count 1   (only the older event)
      get_schema_diff     added 1   app [invoices]
    """
    _migrate(pg)
    for db in ("rgt", "wrg"):
        pg.raw(f"DROP DATABASE IF EXISTS {db}", db="postgres")
        pg.raw(f"CREATE DATABASE {db}", db="postgres")
        pg.raw("CREATE SCHEMA app; CREATE TABLE app.users (id int)", db=db)
    cid = "flap-1"
    pg.raw(f"DELETE FROM schema_snapshots WHERE cluster_id = '{cid}'")

    assert _run_in(pg, "rgt", cid, "2026-08-01T00:00:00+00:00")["baselines"] == 2
    # ONE cycle looks at the wrong database.
    assert _run_in(pg, "wrg", cid, "2026-08-01T00:05:00+00:00")["scope_status"] == "rescoped"
    # The config is fixed, and a REAL CREATE TABLE happened in the right one.
    pg.raw("CREATE TABLE app.invoices (id int, total numeric)", db="rgt")
    out = _run_in(pg, "rgt", cid, "2026-08-01T00:10:00+00:00")
    assert out["changes"] == 1, out
    # `public` had no change and its newest row is the foreign one, so it is
    # re-baselined (S7) rather than heartbeat-only: the readers resolve the NEWEST
    # row per schema, so leaving the foreign row newest keeps the schema unconfirmed.
    assert out["baselines"] == 1, out
    stored = _latest(pg, cid, "app")
    assert json.loads(stored["d"])["added"] == ["invoices"], stored

    # BOTH FAMILIES OF CONSUMER NOW AGREE, which is the property that failed.
    hist = sh.get_schema_history_impl(pg, cluster_id=cid, days=36500)
    assert any(json.loads(c["changes"]).get("added") == ["invoices"]
               for c in hist["changes"]), hist["changes"]
    diff = sd.get_schema_diff_impl(pg, cluster_id=cid)
    assert [d["added"] for d in diff["diffs"] if d["schema_name"] == "app"] == \
        [["invoices"]], diff["diffs"]
    # The fifth consumer, api/dashboard `_timeline`, replays the same stored row. It
    # is driven on the harness that has event_log and audit_log too:
    # tests/unit/api/test_dashboard_schema_changes_real_pg.py.
    examined, skipped = {}, []
    drc._collect_schema_changes(pg, cid, "2026-08-01T00:09:00+00:00",
                                "2026-08-01T00:11:00+00:00", None, 60,
                                examined, skipped)
    assert examined.get("schema_changes") == 1, (examined, skipped)
