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

_PORT = "55433"
# A unix socket path over ~103 bytes is refused, and the pytest tmp path is much
# longer than that, so the data dir goes somewhere short and we talk TCP.
_PGDATA = os.path.join(tempfile.gettempdir(), "dbops_e4_pg")


@pytest.fixture(scope="module")
def pg():
    shutil.rmtree(_PGDATA, ignore_errors=True)
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
        subprocess.run([_PGCTL, "-D", _PGDATA, "-m", "immediate", "stop"],
                       capture_output=True)
        shutil.rmtree(_PGDATA, ignore_errors=True)


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
