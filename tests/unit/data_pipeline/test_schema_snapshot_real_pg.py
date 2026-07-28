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
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION = _ROOT / "data-pipeline" / "schema_migrator" / "sql" / "schema_v26.sql"
_COLLECTORS = _ROOT / "data-pipeline" / "etl_collector" / "collectors"

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

def _free_port():
    """Ask the kernel. A hardcoded port collides with a sibling real-PG fixture
    in the same run and with a postmaster an aborted run left behind."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


_PORT = _free_port()
# A unix socket path over ~103 bytes is refused, and the pytest tmp path is much
# longer than that, so the data dir goes somewhere short and we talk TCP.
# PID-scoped: two concurrent runs must not share one datadir, and the teardown of
# one must not delete the datadir the other is still serving from.
_PGDATA = os.path.join(tempfile.gettempdir(), f"dbops_e4_pg_{os.getpid()}")


def _stop_and_remove():
    """Stop FIRST, then remove. rmtree under a live postmaster leaves it running
    on a datadir that no longer exists, and every later fixture in that process
    then fails to start: 34 fixture ERRORs once got written off as flake."""
    subprocess.run([_PGCTL, "-D", _PGDATA, "-m", "immediate", "stop"],
                   capture_output=True)
    shutil.rmtree(_PGDATA, ignore_errors=True)


@pytest.fixture(scope="module")
def pg():
    _stop_and_remove()
    os.makedirs(_PGDATA, exist_ok=True)
    subprocess.run([_INITDB, "-D", _PGDATA, "-U", "dbops", "--auth=trust"],
                   check=True, capture_output=True)
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

    def raw(self, sql, db="postgres"):
        out = subprocess.run(
            [_PSQL, "-h", "127.0.0.1", "-p", _PORT, "-U", "dbops", "-d", db,
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
    pg.raw(_MIGRATION.read_text())
    cols = {r[0]: r[1] for r in pg.raw(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'schema_snapshots' ORDER BY column_name")}
    assert cols == {
        "cluster_id": "text",
        "diff_from_previous_json": "jsonb",
        "schema_name": "text",
        "snapshot_time": "timestamp with time zone",
        "tables_json": "jsonb",
    }
    # The collector's ON CONFLICT target and the readers' lookup key.
    idx = pg.raw("SELECT indexdef FROM pg_indexes WHERE tablename = 'schema_snapshots'")
    defs = " ".join(r[0] for r in idx)
    assert "UNIQUE" in defs and "cluster_id, schema_name, snapshot_time" in defs
    assert "brin" in defs  # purge support
    # Re-applying must be a no-op: the migrator re-runs every file every deploy.
    pg.raw(_MIGRATION.read_text())


# ===========================================================================
# Collector: real catalog, real INSERT, store-on-change
# ===========================================================================


def test_collector_sql_runs_on_real_catalog_and_stores_baseline(pg):
    pg.raw(_MIGRATION.read_text())
    pg.raw("DELETE FROM schema_snapshots")
    pg.raw("DROP SCHEMA IF EXISTS app CASCADE; CREATE SCHEMA app")
    pg.raw("CREATE TABLE app.users (id int, email text, name text)")
    pg.raw("CREATE TABLE app.orders (id int, amount numeric)")
    pg.raw("CREATE VIEW app.v_users AS SELECT id FROM app.users")

    api = _DataApi(pg)
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


def test_reader_sql_executes_and_finds_the_real_change(pg):
    """schema_diff's implicit LEFT JOIN + schema_history's window filter +
    diagnose_root_cause's window filter, all as shipped."""
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

    examined, skipped = {}, []
    drc._collect_schema_changes(pg, "baseline-only-1", "2026-07-02T00:00:00+00:00",
                               "2026-07-03T00:00:00+00:00", None, 60, examined, skipped)
    assert skipped == ["schema_changes"]
    assert "schema_changes" not in examined


def test_zero_snapshots_is_not_reported_as_no_changes(pg):
    """The table EXISTS and is empty for this cluster: the exact state where the
    old readers said 'no schema changes'."""
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
    """Two+ snapshots exist but none in the asked-about window: this IS a real
    negative, and it must be distinguishable from the two states above."""
    hist = sh.get_schema_history_impl(pg, cluster_id="prod-pg-1", days=1)
    assert hist["status"] == "no_changes"
    assert hist["collection_coverage"]["snapshots_stored"] >= 2
    assert hist["collection_coverage"]["first_snapshot"]


def test_purge_keeps_the_latest_snapshot_per_schema(pg):
    """The retention DELETE must never take the current baseline: it is the only
    thing the next change has to be diffed against."""
    # Load by path under a unique module name: several assets in this repo have a
    # top-level handler.py and a bare `import handler` picks whichever one another
    # test put on sys.path first.
    spec = importlib.util.spec_from_file_location(
        "_e4_etl_handler", _ROOT / "data-pipeline" / "etl_collector" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    SCHEMA_SNAPSHOTS_PURGE_SQL = mod.SCHEMA_SNAPSHOTS_PURGE_SQL

    # Age every row past the cutoff, then run the REAL purge statement.
    pg.raw("UPDATE schema_snapshots SET snapshot_time = snapshot_time - INTERVAL '200 days'")
    before = int(pg.execute("SELECT COUNT(*) AS n FROM schema_snapshots", {}).rows[0]["n"])
    pg.raw(SCHEMA_SNAPSHOTS_PURGE_SQL)
    rows = pg.execute(
        "SELECT cluster_id, schema_name, COUNT(*) AS n FROM schema_snapshots "
        "GROUP BY cluster_id, schema_name", {}).rows
    assert before > len(rows), "purge deleted nothing, so it proves nothing"
    # Exactly one row survives per (cluster, schema): the latest.
    assert rows and all(int(r["n"]) == 1 for r in rows)


# ===========================================================================
# A schema that goes to ZERO tables, and a schema that disappears entirely.
# Store-on-change means the last blob written stands as `latest` until something
# replaces it, so a schema the collector stops SEEING keeps serving its dropped
# tables as existing, forever, on all three readers.
# ===========================================================================


def _run_collector(pg, cluster_id, ts):
    api = _DataApi(pg)
    return collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y",
                                      cluster_id, "postgres", snapshot_ts=ts)


def _latest(pg, cluster_id, schema_name):
    return pg.execute(
        "SELECT tables_json::text AS t, diff_from_previous_json::text AS d "
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
    assert out["vanished"] == 0  # the schema still exists; nothing was inferred
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
    examined, skipped = {}, []
    got = drc._collect_schema_changes(pg, "zero-1", "2026-07-20T00:04:00+00:00",
                                      "2026-07-20T00:06:00+00:00", None, 60,
                                      examined, skipped)
    assert skipped == []
    assert examined["schema_changes"] == 1
    assert got[0]["evidence"]["schema_name"] == "zeroed"

    # 288 runs a day: an already-empty schema must not be re-recorded.
    assert _run_collector(pg, "zero-1", "2026-07-20T00:10:00+00:00")["snapshots_written"] == 0


def test_a_dropped_schema_is_recorded_as_dropped(pg):
    """DROP SCHEMA leaves no row to iterate at all, so this one IS inferred from
    absence against TRACKED_SQL."""
    pg.raw("DROP SCHEMA IF EXISTS wiped CASCADE; CREATE SCHEMA wiped")
    pg.raw("CREATE TABLE wiped.t1 (id int); CREATE TABLE wiped.t2 (id int)")
    assert _run_collector(pg, "wipe-1", "2026-07-21T00:00:00+00:00")["baselines"] >= 1

    pg.raw("DROP SCHEMA wiped CASCADE")
    out = _run_collector(pg, "wipe-1", "2026-07-21T00:05:00+00:00")
    assert out["vanished"] == 1
    assert out["vanished_unconfirmed"] == 0
    row = _latest(pg, "wipe-1", "wiped")
    assert json.loads(row["t"]) == {}
    assert json.loads(row["d"])["dropped"] == ["t1", "t2"]
    # And it stays dropped without filing a change row every 5 minutes.
    assert _run_collector(pg, "wipe-1", "2026-07-21T00:10:00+00:00")["vanished"] == 0


def test_a_catalog_read_that_returned_nothing_never_invents_a_mass_drop(pg):
    """Zero schemas returned is what a wrong database, a dead session or a total
    privilege loss looks like, and it cannot be told apart from "every schema was
    dropped". Inventing a mass drop out of it would be worse than the bug."""
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
    assert out["vanished"] == 0
    assert out["vanished_unconfirmed"] >= 1
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
