"""REAL-ENGINE test for `GET /api/dashboard/{id}/schema-changes`.

The panel used to derive table CREATED / DROPPED from `table_stats`, whose two
producers both cap their catalog read at the 100 largest tables
(pg_table_stats.py, mysql_table_stats.py). Three defects followed, and all three
were reproduced on a live PostgreSQL before being fixed. The OLD SQL is kept
verbatim in `_OLD_SQL` below and executed side by side with the new reader, so
every claim here is a measured difference between two statements the same server
ran, not a text comparison against a fake's canned rows:

  1. `dropped` was UNREACHABLE. `baseline` (newest row older than :days) is a
     subset of an UNBOUNDED `latest`, so `l.table_name IS NULL` could never be
     true. A genuinely dropped table was reported as NOTHING AT ALL.
  2. `created` fired on TOP-100 ENTRANTS. A table that merely grew into the
     largest 100 has no row older than :days, which the CASE called 'created'.
  3. `latest` had no time bound, so a value from an arbitrarily old snapshot was
     presented as the current row count with nothing marking it stale.

And a fourth state the old shape could not express: a cluster whose collection
stopped entirely returns ZERO rows, which the panel renders identically to
"nothing changed" (baseline and latest collapse onto the same row, so the delta
is 0 and every row is filtered out).

Section 9 adds the STATE MATRIX: the cells a real server can produce, driven by
the shipped statements and followed through the panel's parsed branch chain to the
sentence an operator reads. The full cross-product of the four signals lives in
tests/unit/api/test_schema_changes_panel_states.py, which also holds the panel
model this file imports.

Two cells were added by the fifth pass and both were reproduced on this harness
before being fixed:
  5. `partial_window`: a snapshot history that STARTS inside the window. Measured
     pre-fix with 3 snapshots from 7 days ago and days=30, baseline_outside_window
     TRUE, row deltas ok, collection fresh -> status "no_changes" and the panel
     headline "이 구간에서 감지된 변경 없음", i.e. a 30-day question answered from 7
     days of data reached the one status licensed to read as an absence of change.
  6. an UNREADABLE schema_snapshots plus an empty table_stats reported
     "not_collected", whose note says both sources hold no row for this cluster.
     The snapshot read had raised, so that sentence was a negative the data could
     not support.
This file also follows a real DROP all the way to the CELL of the panel that
prints how many rows it lost (`panel_change_row`): the operator-facing half of the
positive branch, which nothing modelled until the fifth pass.

ENGINE: PostgreSQL from the local install. The production DDL in
data-pipeline/schema_migrator/sql/ is applied in the migrator's own numeric
order. Skipped, not faked, when no initdb/pg_ctl/psql is on the machine.

The port comes from the KERNEL. It used to be `56000 + os.getpid() % 3000`, and
the guess was MEASURED to fail: with that port held by anything else, pg_ctl start
raises before the fixture yields, so the try/finally never runs and the datadir is
left behind for the next run to trip over. Observed on this machine, one process
squatting the computed port: `pg_ctl: could not start server`, datadir still
present after the raise; the kernel-assigned port started normally under the same
squatter. Two concurrent runs of this module then pass (32 + 32) and leave no
datadir and no postmaster behind.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SQLDIR = _ROOT / "data-pipeline" / "schema_migrator" / "sql"
_DASHBOARD_DIR = _ROOT / "api" / "dashboard"
_COLLECTORS = _ROOT / "data-pipeline" / "etl_collector" / "collectors"

sys.path.insert(0, str(_DASHBOARD_DIR))
sys.path.insert(0, str(_COLLECTORS.parent))

os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_spec = importlib.util.spec_from_file_location(
    "dashboard_handler_schema_changes", _DASHBOARD_DIR / "handler.py"
)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

from collectors.schema_snapshot import collect_pg_schema_snapshot  # noqa: E402

# The panel's branch chain, parsed once there and reused here so a real DDL event
# on a real engine is followed all the way to the sentence an operator reads.
# Importing rather than re-parsing keeps ONE model of the panel: two copies drift,
# and a drifted copy is how "the fix stops at the API boundary" survives a suite.
from tests.unit.api.test_schema_changes_panel_states import (  # noqa: E402
    _NEUTRAL,
    panel_change_row,
    panel_verdict,
)

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
    reason="no local PostgreSQL (initdb/pg_ctl/psql), real-engine test skipped",
)

def _free_port():
    """Ask the KERNEL. `56000 + os.getpid() % 3000` was a guess: whatever already
    holds that port reproduces the exact cascade this harness family was fixed
    for, and it does not self-heal, because pg_ctl start raises BEFORE the fixture
    yields, so the try/finally never runs and the datadir is left behind for the
    next run to trip over. Same as tests/unit/data_pipeline/
    test_schema_snapshot_real_pg.py, which solved it properly first."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


_PORT = _free_port()
# PID-scoped: two concurrent runs must not share one datadir, and the teardown of
# one must not delete the datadir the other is still serving from.
_PGDATA = os.path.join(tempfile.gettempdir(), f"dbops_schema_changes_pg_{os.getpid()}")


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
    exists, and every later fixture in the process then fails to start: 34 fixture
    ERRORs once got written off as flake. Called on setup as well as teardown, which
    is the only self-healing there is for a run that died before its finally.

    `ignore_errors=True` after an UNCHECKED stop is exactly how that state was reached
    a second time. MEASURED on the shipped version of the sibling copy of this
    function, driven with a live server whose postmaster.pid had been removed (what a
    previous masked rmtree leaves behind): it returned with no exception, the
    postmaster was still alive, still serving the port, and the datadir was gone. So
    the stop is VERIFIED against the port, and a server that did not stop raises HERE,
    with its datadir intact so it can still be stopped by hand or by the next setup
    call. Same treatment as tests/unit/data_pipeline/test_schema_snapshot_real_pg.py:
    these two are copies of one harness and a fix to one that skips the other leaves
    the leak in the run.
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

# `:name` binds, but NOT the `::type` cast that follows one.
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
    def raw(self, sql):
        out = subprocess.run(
            [_PSQL, "-h", "127.0.0.1", "-p", _PORT, "-U", "dbops", "-d", "postgres",
             "-v", "ON_ERROR_STOP=1", "-tA", "-F", "\x1f", "-c", sql],
            capture_output=True, text=True)
        if out.returncode != 0:
            raise AssertionError(f"psql failed: {out.stderr.strip()}\nSQL: {sql[:400]}")
        return [ln.split("\x1f") for ln in out.stdout.splitlines() if ln != ""]

    def query(self, sql, params=None):
        """Mimics api/dashboard/handler.py::_make_query: named binds in, list of
        name-keyed dict rows out, and a jsonb column handed back as a STRING the
        way the Data API's stringValue branch does."""
        bound = _BIND.sub(lambda m: _lit((params or {})[m.group(1)]), sql)
        rows = self.raw(f"SELECT row_to_json(_rj) FROM ({bound}) _rj")
        out = []
        for r in rows:
            row = json.loads(r[0])
            out.append({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                        for k, v in row.items()})
        return out


class _DataApi:
    """rds-data client shape over the same server, for the collector."""

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
        if not stripped.upper().startswith(("SELECT", "WITH")):
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
        srv = _Server()
        # Production DDL, in the migrator's numeric-aware order (handler.py
        # _ver_key): schema.sql, schema_v2, ... schema_v26.
        def _ver_key(fname):
            m = re.match(r"^schema(?:_v(\d+))?\.sql$", fname)
            return (1, 0) if not m else (0, int(m.group(1) or 0))
        files = sorted((f for f in os.listdir(_SQLDIR)
                        if f.startswith("schema") and f.endswith(".sql")), key=_ver_key)
        # ON_ERROR_STOP is deliberately OFF for one reason only: schema_v21
        # requires the pgvector extension, which a stock local PostgreSQL does
        # not ship. Every other statement must apply, so EVERY error the server
        # reports has to be a pgvector one. Anything else fails the fixture
        # rather than quietly leaving a relation missing.
        errors = []
        for f in files:
            out = subprocess.run(
                [_PSQL, "-h", "127.0.0.1", "-p", _PORT, "-U", "dbops", "-d", "postgres",
                 "-f", str(_SQLDIR / f)],
                capture_output=True, text=True)
            errors += [f"{f}: {ln}" for ln in (out.stdout + out.stderr).splitlines()
                       if "ERROR:" in ln]
        unexpected = [e for e in errors if "vector" not in e]
        assert not unexpected, f"migrations failed for non-pgvector reasons: {unexpected}"
        # The two relations this panel reads must exist, or every assertion below
        # would be vacuously true.
        assert srv.raw("SELECT to_regclass('table_stats')::text, "
                       "to_regclass('schema_snapshots')::text") == [["table_stats",
                                                                     "schema_snapshots"]]
        yield srv
    finally:
        _stop_and_remove()


# The SHIPPED-BEFORE statement, verbatim. Executed against the same rows as the
# new reader so "the old one could not see this" is a measurement.
_OLD_SQL = (
    "WITH latest AS ("
    "  SELECT DISTINCT ON (schema_name, table_name) "
    "    schema_name, table_name, n_live_tup, snapshot_time "
    "  FROM table_stats "
    "  WHERE cluster_id = :cid "
    "  ORDER BY schema_name, table_name, snapshot_time DESC"
    "), "
    "baseline AS ("
    "  SELECT DISTINCT ON (schema_name, table_name) "
    "    schema_name, table_name, n_live_tup, snapshot_time "
    "  FROM table_stats "
    "  WHERE cluster_id = :cid "
    "  AND snapshot_time < NOW() - (:days || ' days')::interval "
    "  ORDER BY schema_name, table_name, snapshot_time DESC"
    ") "
    "SELECT "
    "  COALESCE(l.schema_name, b.schema_name) AS schema_name, "
    "  COALESCE(l.table_name, b.table_name) AS table_name, "
    "  b.n_live_tup AS baseline_rows, "
    "  l.n_live_tup AS current_rows, "
    "  CASE "
    "    WHEN b.table_name IS NULL THEN 'created' "
    "    WHEN l.table_name IS NULL THEN 'dropped' "
    "    ELSE 'changed' "
    "  END AS change_type, "
    "  b.snapshot_time AS baseline_time, "
    "  l.snapshot_time AS current_time "
    "FROM latest l "
    "FULL OUTER JOIN baseline b "
    "  ON l.schema_name = b.schema_name AND l.table_name = b.table_name "
    "WHERE b.table_name IS NULL "
    "   OR l.table_name IS NULL "
    "   OR (b.n_live_tup IS NOT NULL AND l.n_live_tup IS NOT NULL "
    "       AND ABS(l.n_live_tup - b.n_live_tup) > GREATEST(b.n_live_tup * 0.5, 1000)) "
    "ORDER BY change_type, schema_name, table_name "
    "LIMIT 50"
)


def _meta(pg, cluster_id, engine="aurora-postgresql"):
    """The cluster_meta row the ETL writes BEFORE any of these producers run
    (etl_collector/handler.py collects meta first). The panel resolves the DIALECT
    from it, because schema snapshots are PostgreSQL-only: MySQL's information_schema
    is privilege-filtered, so a REVOKE and a DROP are the same read and no
    created/dropped claim is possible there. Seeding it wherever data is seeded is
    what production looks like, not a convenience."""
    pg.raw("INSERT INTO cluster_meta (cluster_id, account_id, region, engine) VALUES ("
           f"{_lit(cluster_id)}, '123456789012', 'ap-northeast-2', {_lit(engine)}) "
           "ON CONFLICT (cluster_id) DO UPDATE SET engine = EXCLUDED.engine")


def _stat(pg, cluster, table, rows, age_days, schema="app", bytes_=1000):
    _meta(pg, cluster)
    pg.raw(
        "INSERT INTO table_stats (cluster_id, snapshot_time, schema_name, table_name, "
        " n_live_tup, total_bytes) VALUES ("
        f"{_lit(cluster)}, NOW() - INTERVAL '1 day' * {float(age_days)}, "
        f"{_lit(schema)}, {_lit(table)}, {rows}, {bytes_})"
    )


def _ago(days):
    """Snapshot timestamps have to be NOW-relative: the reader's baseline is the
    newest snapshot at or before NOW() - :days, so a fixture stamped at a fixed
    calendar date drifts out of the window and silently compares the latest
    snapshot against itself."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _by_type(result):
    out = {}
    for c in result["changes"]:
        out.setdefault(c["change_type"], set()).add(f"{c['schema_name']}.{c['table_name']}")
    return out


# ===========================================================================
# 1. A GENUINELY DROPPED TABLE IS REPORTED AS DROPPED
#    End to end: real DDL on a real catalog -> the real collector -> the reader.
# ===========================================================================


def test_real_drop_is_reported_and_the_old_sql_could_not_see_it(pg):
    cid = "drop-1"
    pg.raw("DROP SCHEMA IF EXISTS app CASCADE; CREATE SCHEMA app")
    pg.raw("CREATE TABLE app.users (id int, email text)")
    pg.raw("CREATE TABLE app.orders (id int, amount numeric)")

    # table_stats history: both tables 10 days ago, users again just now. `orders`
    # has NO row after the drop, which is all the old SQL had to go on.
    _stat(pg, cid, "users", 5000, 10)
    _stat(pg, cid, "orders", 9000, 10)
    _stat(pg, cid, "users", 5100, 0)

    api = _DataApi(pg)
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(10))
    pg.raw("DROP TABLE app.orders")
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(1 / 24.0))

    got = handler._schema_changes(pg.query, cid, 7)
    types = _by_type(got)
    assert "app.orders" in types.get("dropped", set()), got
    assert got["status"] == "ok"
    assert got["ddl_detection"]["status"] == "ok"
    assert got["ddl_detection"]["source"] == "schema_snapshots"
    assert got["ddl_detection"]["schemas_compared"] >= 1
    # The dropped row carries its LAST OBSERVED count, which is what the panel
    # renders as "N 행 손실".
    row = [c for c in got["changes"] if c["table_name"] == "orders"][0]
    assert row["baseline_rows"] == 9000
    assert row["current_rows"] is None
    assert row["source"] == "schema_snapshots"
    # ...and reaches the CELL of the panel that prints it. The payload half of
    # this was already asserted; the operator-facing half was modelled by nothing,
    # so the whole positive branch of the panel could be deleted green. Same
    # single panel model as the empty-verdict chain, so a real DROP on a real
    # engine is followed to the text a DBA reads.
    cell = panel_change_row(row)
    assert "행 손실" in cell, cell
    assert "value={baseline}" in cell, (
        "the dropped cell must render the LAST OBSERVED count; current_rows is "
        "None for every drop, so a cell reading it prints 행 수 미상 always")

    # DEFECT 1, measured: the shipped statement returns no `dropped` row for the
    # same table on the same rows, because `baseline` is a subset of `latest`.
    old = pg.query(_OLD_SQL, {"cid": cid, "days": "7"})
    assert not [r for r in old if r["change_type"] == "dropped"], old
    assert "orders" not in [r["table_name"] for r in old], old


# ===========================================================================
# 2. TOP-100 BOUNDARY CROSSINGS ARE NOT DDL, IN EITHER DIRECTION
# ===========================================================================


def test_table_entering_the_top_100_is_not_reported_as_created(pg):
    """`entrant` existed all along; it only became big enough for the capped
    collector to record it 3 days ago, so it has no row older than :days."""
    cid = "cap-in-1"
    pg.raw("DROP SCHEMA IF EXISTS cap CASCADE; CREATE SCHEMA cap")
    pg.raw("CREATE TABLE cap.steady (id int)")
    pg.raw("CREATE TABLE cap.entrant (id int)")

    _stat(pg, cid, "steady", 5000, 10, schema="cap")
    _stat(pg, cid, "steady", 5100, 0, schema="cap")
    _stat(pg, cid, "entrant", 400000, 3, schema="cap")
    _stat(pg, cid, "entrant", 410000, 0, schema="cap")

    api = _DataApi(pg)
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(10))
    pg.raw("CREATE TABLE cap.really_new (id int)")
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(1 / 24.0))

    got = handler._schema_changes(pg.query, cid, 7)
    types = _by_type(got)
    assert "cap.entrant" not in types.get("created", set()), got
    assert "cap.entrant" not in types.get("dropped", set()), got
    # A REAL creation in the same run still lands, so the guard is not just
    # suppressing everything.
    assert "cap.really_new" in types.get("created", set()), got

    # DEFECT 2, measured: the shipped statement called the entrant 'created'.
    old = pg.query(_OLD_SQL, {"cid": cid, "days": "7"})
    assert {"entrant"} == {r["table_name"] for r in old if r["change_type"] == "created"}, old


def test_table_leaving_the_top_100_is_not_reported_as_dropped(pg):
    """`leaver` stopped being collected 3 days ago because it fell out of the
    100 largest. It still EXISTS, and the complete snapshot map proves it."""
    cid = "cap-out-1"
    pg.raw("DROP SCHEMA IF EXISTS out100 CASCADE; CREATE SCHEMA out100")
    pg.raw("CREATE TABLE out100.leaver (id int)")
    pg.raw("CREATE TABLE out100.steady (id int)")

    _stat(pg, cid, "leaver", 900000, 10, schema="out100")
    _stat(pg, cid, "leaver", 100, 3, schema="out100")   # last row: shrank, then gone
    _stat(pg, cid, "steady", 5000, 10, schema="out100")
    _stat(pg, cid, "steady", 5100, 0, schema="out100")

    api = _DataApi(pg)
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(10))
    pg.raw("CREATE TABLE out100.marker (id int)")  # forces a second snapshot
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(1 / 24.0))

    got = handler._schema_changes(pg.query, cid, 7)
    types = _by_type(got)
    assert "out100.leaver" not in types.get("dropped", set()), got
    assert "out100.leaver" not in types.get("created", set()), got
    # Its row-count collapse IS a real observation and stays reportable.
    assert "out100.leaver" in types.get("changed", set()), got


def test_a_dropped_tables_leftover_table_stats_row_is_not_a_row_delta(pg):
    """Once the complete map says the table is gone, its stale table_stats row
    must not also surface as a `changed` row: one event, one line."""
    got = handler._schema_changes(pg.query, "drop-1", 7)
    changed = _by_type(got).get("changed", set())
    assert "app.orders" not in changed, got
    assert len([c for c in got["changes"] if c["table_name"] == "orders"]) == 1


# ===========================================================================
# 3. NO USABLE HISTORY SAYS SO
# ===========================================================================


def test_one_table_is_one_line_even_when_both_sources_have_something_to_say(pg):
    """A table the snapshot diff calls CREATED can still have older table_stats
    rows: it was dropped and recreated, or snapshot history is younger than
    table_stats history. Reporting it as created AND as a row delta would show a
    DBA two lines for one table."""
    cid = "recreate-1"
    pg.raw("DROP SCHEMA IF EXISTS rec CASCADE; CREATE SCHEMA rec")
    pg.raw("CREATE TABLE rec.keeper (id int)")
    _stat(pg, cid, "t", 5000, 10, schema="rec")        # earlier life
    _stat(pg, cid, "t", 900000, 0, schema="rec")       # after the recreate
    api = _DataApi(pg)
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(10))
    pg.raw("CREATE TABLE rec.t (id int)")
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(1 / 24.0))

    got = handler._schema_changes(pg.query, cid, 7)
    rows = [c for c in got["changes"] if c["table_name"] == "t"]
    assert len(rows) == 1, got["changes"]
    assert rows[0]["change_type"] == "created"


def test_cluster_with_no_history_at_all_says_not_collected(pg):
    # Registered and meta-collected, nothing produced yet. Without the meta row the
    # DIALECT is unknown, which is `unavailable` and its own cell below.
    _meta(pg, "never-collected-1")
    got = handler._schema_changes(pg.query, "never-collected-1", 7)
    assert got["changes"] == []
    assert got["status"] == "not_collected"
    assert got["collection"]["status"] == "no_data"
    assert got["collection"]["last_collected"] is None
    assert got["ddl_detection"]["status"] == "not_collected"
    assert got["ddl_detection"]["snapshots_stored"] == 0
    assert "변경이 없다는 뜻이 아닙니다" in got["note"]

    # The OLD reader answered the same state with a bare empty list, which the
    # panel renders as "감지된 스키마 변경 없음".
    assert pg.query(_OLD_SQL, {"cid": "never-collected-1", "days": "7"}) == []


def test_table_stats_only_cluster_cannot_claim_no_ddl(pg):
    """table_stats history but no snapshots: row deltas work, DDL is UNKNOWN and
    must be reported as unknown rather than as an absence of DDL."""
    cid = "stats-only-1"
    _stat(pg, cid, "t", 1000, 10)
    _stat(pg, cid, "t", 1000, 0)

    got = handler._schema_changes(pg.query, cid, 7)
    assert got["row_deltas"]["status"] == "ok"
    assert got["row_deltas"]["tables_compared"] == 1
    assert got["ddl_detection"]["status"] == "not_collected"
    assert got["ddl_detection"]["schemas_compared"] == 0
    assert "DDL 변경이 없다는 뜻이 아닙니다" in got["note"]
    # `partial`, not `no_changes`: row deltas are a real negative, DDL is silence,
    # and the HEADLINE has to carry that. It used to be "no_changes", so the
    # sentence an operator read on a cluster whose DDL nobody could judge was
    # "이 구간에서 감지된 변경 없음". Measured: status was `no_changes` here before
    # the ddl_complete change and is `partial` after.
    assert got["status"] == "partial"
    assert got["changes"] == []
    assert _NEUTRAL not in panel_verdict(got)
    # The row-delta cap is stated, never implied.
    assert "상위 100개" in got["note"]


def test_baseline_only_snapshot_is_not_a_comparison(pg):
    cid = "baseline-only-1"
    pg.raw("DROP SCHEMA IF EXISTS solo CASCADE; CREATE SCHEMA solo")
    pg.raw("CREATE TABLE solo.t (id int)")
    _stat(pg, cid, "t", 10, 0, schema="solo")
    api = _DataApi(pg)
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(10))

    got = handler._schema_changes(pg.query, cid, 7)
    assert got["ddl_detection"]["status"] == "baseline_only"
    assert got["ddl_detection"]["schemas_compared"] == 0
    assert got["ddl_detection"]["baseline_only_schemas"], got
    assert got["ddl_detection"]["snapshots_stored"] >= 1
    assert "스냅샷 2개가" in got["note"]


def test_history_shorter_than_the_window_reports_the_span_it_had(pg):
    """days=90 over a history that starts inside the window: the diff is real but
    it does not cover 90 days, and the payload has to say which span it used."""
    got = handler._schema_changes(pg.query, "drop-1", 90)
    assert got["ddl_detection"]["partial_window_schemas"], got
    assert got["ddl_detection"]["first_snapshot"]
    assert "구간만" in got["note"]


# ===========================================================================
# 4. STALE DATA IS LABELLED, NOT PRESENTED AS CURRENT
# ===========================================================================


def test_stale_collection_is_labelled_with_its_age(pg):
    """ETL stopped 2 days ago. The change inside the window is still real, but
    `current_rows` is 48h old and the payload must say so."""
    cid = "stale-1"
    _stat(pg, cid, "t", 5000, 10)
    _stat(pg, cid, "t", 90000, 2)

    got = handler._schema_changes(pg.query, cid, 7)
    assert _by_type(got).get("changed") == {"app.t"}
    assert got["collection"]["status"] == "stale"
    assert 47.0 <= got["collection"]["age_hours"] <= 49.0, got["collection"]
    assert got["collection"]["last_collected"]
    assert "지금 값이 아닙니다" in got["note"]

    # DEFECT 3, measured: the shipped statement emitted the same 48h-old value
    # with nothing anywhere in the payload marking it stale.
    old = pg.query(_OLD_SQL, {"cid": cid, "days": "7"})
    assert [r["change_type"] for r in old] == ["changed"]
    assert set(old[0]) == {"schema_name", "table_name", "baseline_rows", "current_rows",
                           "change_type", "baseline_time", "current_time"}


def test_fresh_collection_is_labelled_fresh(pg):
    """Mutation guard for the staleness threshold: a cluster collected minutes
    ago must NOT be called stale, or the label means nothing."""
    cid = "fresh-1"
    _stat(pg, cid, "t", 5000, 10)
    _stat(pg, cid, "t", 90000, 0)
    got = handler._schema_changes(pg.query, cid, 7)
    assert got["collection"]["status"] == "fresh"
    assert got["collection"]["age_hours"] is not None
    assert got["collection"]["age_hours"] < 1
    assert "지금 값이 아닙니다" not in got["note"]


def test_snapshots_without_any_table_stats_cannot_date_their_verdict(pg):
    """schema_snapshots is store-on-change, so it normally writes 0 rows/day and
    cannot answer "is collection still running". With no table_stats row for the
    cluster there is no freshness signal at all, and an undatable verdict has to
    say it is undatable rather than read as "currently no changes"."""
    cid = "no-stats-1"
    _meta(pg, cid)
    pg.raw("DROP SCHEMA IF EXISTS nostats CASCADE; CREATE SCHEMA nostats")
    pg.raw("CREATE TABLE nostats.t (id int)")
    api = _DataApi(pg)
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(10))
    pg.raw("CREATE TABLE nostats.t2 (id int)")
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(1 / 24.0))

    got = handler._schema_changes(pg.query, cid, 7)
    assert "nostats.t2" in _by_type(got).get("created", set()), got
    assert got["collection"]["status"] == "no_data"
    assert got["collection"]["age_hours"] is None
    assert "언제 기준인지 확인할 수 없습니다" in got["note"]


def test_fully_stale_cluster_does_not_read_as_nothing_changed(pg):
    """Collection stopped 40 days ago. Every row predates a 7-day window, so
    baseline and latest collapse onto the same row and the old SQL returned
    nothing: indistinguishable from a quiet cluster."""
    cid = "dead-etl-1"
    _stat(pg, cid, "t", 10, 80)
    _stat(pg, cid, "t", 100000, 40)

    got = handler._schema_changes(pg.query, cid, 7)
    assert got["changes"] == []
    assert got["status"] == "insufficient_history"
    assert got["collection"]["status"] == "stale"
    assert got["row_deltas"]["status"] == "insufficient_history"
    assert got["row_deltas"]["tables_compared"] == 0
    assert "변경이 없다는 뜻이 아닙니다" in got["note"]

    assert pg.query(_OLD_SQL, {"cid": cid, "days": "7"}) == []


# ===========================================================================
# 5. Contract the panel depends on, and the degraded path
# ===========================================================================


def test_change_rows_keep_the_six_fields_the_panel_renders(pg):
    got = handler._schema_changes(pg.query, "drop-1", 7)
    assert got["changes"]
    for c in got["changes"]:
        assert set(c) >= {"schema_name", "table_name", "baseline_rows", "current_rows",
                          "change_type", "baseline_time", "current_time"}
        assert c["change_type"] in ("created", "dropped", "changed")
    # Display order the SQL's `ORDER BY change_type, schema_name, table_name`
    # produced, preserved so the panel is not reshuffled.
    keys = [(c["change_type"], c["schema_name"], c["table_name"]) for c in got["changes"]]
    assert keys == sorted(keys)
    assert got["total_changes"] >= len(got["changes"])
    assert got["truncated"] is False


def test_rename_is_a_rename_candidate_not_a_drop(pg):
    """The panel and get_schema_diff must describe one DDL event the same way."""
    cid = "rename-1"
    pg.raw("DROP SCHEMA IF EXISTS ren CASCADE; CREATE SCHEMA ren")
    pg.raw("CREATE TABLE ren.audit_old (id int, ts timestamptz)")
    api = _DataApi(pg)
    # audit_old was collected under its old name until yesterday, with a row
    # count that swings far past the delta threshold. compute_diff pairs it into
    # a rename so it is NOT in the dropped set, which makes this the one shape
    # where only the "absent from the complete map" guard can stop the panel
    # reporting a `changed` row for a table that no longer exists.
    _stat(pg, cid, "audit_old", 5000000, 10, schema="ren")
    _stat(pg, cid, "audit_old", 100, 1, schema="ren")
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(10))
    pg.raw("ALTER TABLE ren.audit_old RENAME TO audit_new")
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(1 / 24.0))

    got = handler._schema_changes(pg.query, cid, 7)
    types = _by_type(got)
    assert "ren.audit_old" not in types.get("dropped", set()), got
    assert "ren.audit_new" not in types.get("created", set()), got
    assert {"from": "audit_old", "to": "audit_new", "schema_name": "ren"} in \
        got["ddl_detection"]["rename_candidates"], got["ddl_detection"]
    assert "ren.audit_old" not in _by_type(got).get("changed", set()), got
    assert not [c for c in got["changes"] if c["table_name"] == "audit_old"], got
    # A rename is a schema CHANGE. It is not in `changes` (it is a pair, not a
    # row with counts), so the status must not fall through to "no_changes".
    assert got["status"] == "ok", got["status"]


def test_missing_schema_snapshots_table_degrades_instead_of_500(pg):
    """schema_snapshots ships in schema_v26. A cache DB that has not run the
    migrator must still answer, with DDL marked unavailable and no exception
    text anywhere in the payload."""
    cid = "no-v26-1"
    _stat(pg, cid, "t", 5000, 10)
    _stat(pg, cid, "t", 90000, 0)

    def query(sql, params=None):
        if "schema_snapshots" in sql:
            raise RuntimeError(
                'relation "schema_snapshots" does not exist; secret '
                "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:dbops-cache-AbCdEf")
        return pg.query(sql, params)

    got = handler._schema_changes(query, cid, 7)
    assert got["ddl_detection"]["status"] == "unavailable"
    assert got["row_deltas"]["status"] == "ok"
    assert _by_type(got).get("changed") == {"app.t"}
    blob = json.dumps(got, default=str)
    assert "secretsmanager" not in blob
    assert "does not exist" not in blob
    assert "RuntimeError" not in blob
    assert "schema_v26" in got["note"]  # the actionable hint, not the exception


# ===========================================================================
# 6. A ROW COMPARED AGAINST ITSELF IS NOT A NEGATIVE
# ===========================================================================


def _snapshot_pair(pg, cid, days="7"):
    # The pair statement is built from SCOPED_ROWS now, so it binds :cluster_id and
    # :read_scope: comparability, not just recency, decides which rows it may see.
    # The scope is RESOLVED the way the handler resolves it rather than assumed,
    # because some fixtures in this module drive the real collector, whose scope is
    # the live database's own oid.
    return pg.query(handler._SCHEMA_SNAPSHOT_PAIRS_SQL,
                    {"cluster_id": cid, "read_scope": _established_scope(pg, cid),
                     "days": days})


def _established_scope(pg, cid):
    from schema_diff_util import ESTABLISHED_SCOPE_SQL
    rows = pg.query(ESTABLISHED_SCOPE_SQL, {"cluster_id": cid})
    return rows[0]["read_scope"] if rows else None


def test_every_snapshot_older_than_the_window_is_not_a_comparison(pg):
    """When EVERY snapshot predates the window, `base` (newest at or before the
    window start) and `latest` (newest overall) are the SAME ROW, so
    compute_diff(X, X) is empty by construction. That is not evidence of an
    unchanged schema, and `baseline_outside_window` is FALSE there, so the
    partial-window note does not fire either."""
    cid = "b4b"
    pg.raw("DROP SCHEMA IF EXISTS b4b CASCADE; CREATE SCHEMA b4b")
    pg.raw("CREATE TABLE b4b.t (id int)")
    _stat(pg, cid, "t", 10, 60, schema="b4b")
    _stat(pg, cid, "t", 20, 40, schema="b4b")

    api = _DataApi(pg)
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(60))
    pg.raw("CREATE TABLE b4b.t2 (id int)")
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(40))

    # MEASURED on the server: the pair the statement resolves for schema b4b is
    # one row against itself.
    rows = [r for r in _snapshot_pair(pg, cid) if r["schema_name"] == "b4b"]
    assert rows, "fixture wrote no snapshot pair row for schema b4b"
    assert rows[0]["baseline_time"] == rows[0]["current_time"], rows[0]
    assert rows[0]["baseline_outside_window"] is False, rows[0]
    assert int(rows[0]["snapshots_for_schema"]) == 2, rows[0]

    got = handler._schema_changes(pg.query, cid, 7)
    assert got["changes"] == []
    assert got["ddl_detection"]["status"] == "outside_window", got["ddl_detection"]
    assert got["ddl_detection"]["schemas_compared"] == 0, got["ddl_detection"]
    assert "b4b" in got["ddl_detection"]["outside_window_schemas"], got["ddl_detection"]
    assert got["ddl_detection"]["partial_window_schemas"] == []
    # The headline may not be "no_changes": nothing inside the window was seen.
    assert got["status"] == "insufficient_history", got
    assert "변경이 없다는 뜻이 아닙니다" in got["note"]
    assert "구간보다 오래되어" in got["note"]


def test_a_real_in_window_comparison_still_reports_ok(pg):
    """Mutation guard for the self-comparison skip: it must not swallow a schema
    whose newest snapshot IS inside the window. Self-contained so it fails on the
    guard rather than on test ordering."""
    cid = "in-window-1"
    pg.raw("DROP SCHEMA IF EXISTS inwin CASCADE; CREATE SCHEMA inwin")
    pg.raw("CREATE TABLE inwin.t (id int)")
    _stat(pg, cid, "t", 10, 10, schema="inwin")
    _stat(pg, cid, "t", 20, 0, schema="inwin")
    api = _DataApi(pg)
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(10))
    pg.raw("CREATE TABLE inwin.t2 (id int)")
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                               "postgres", snapshot_ts=_ago(1 / 24.0))

    got = handler._schema_changes(pg.query, cid, 7)
    assert got["ddl_detection"]["status"] == "ok", got["ddl_detection"]
    assert got["ddl_detection"]["outside_window_schemas"] == []
    assert got["ddl_detection"]["schemas_compared"] >= 1
    assert "inwin.t2" in _by_type(got).get("created", set()), got


# ===========================================================================
# 7. THE INCIDENT TIMELINE READS THE TABLE THAT EXISTS
# ===========================================================================


_SCOPE = "dbops/16384"


def _snap(pg, cid, schema, hours_ago, tables, diff, scope=_SCOPE,
          last_seen="NOW()"):
    """One stored snapshot.

    read_scope and last_seen_at are written by DEFAULT, because a row without them
    is a PRE-v27 row and a pre-v27 row is deliberately comparable to nothing: the
    reader would report `not_comparable` for every cell rather than the cell under
    test. `scope=None` is how a cell asks for the pre-v27 shape on purpose.
    """
    _meta(pg, cid)
    pg.raw(
        "INSERT INTO schema_snapshots (cluster_id, snapshot_time, schema_name, "
        " tables_json, diff_from_previous_json, read_scope, last_seen_at) VALUES ("
        f"{_lit(cid)}, NOW() - INTERVAL '1 hour' * {float(hours_ago)}, {_lit(schema)}, "
        f"{_lit(json.dumps(tables))}::jsonb, {_lit(json.dumps(diff))}::jsonb, "
        f"{_lit(scope)}, {'NULL' if last_seen is None else last_seen})"
    )


def test_timeline_ddl_comes_from_schema_snapshots(pg):
    """`schema_changes` is a table NO migration creates, so the timeline's DDL
    category was permanently empty and the failure was swallowed with a "skip
    silently" comment. schema_snapshots is where this tier's DDL lives."""
    cid = "tl-1"
    _snap(pg, cid, "app", 2, {"users": ["id"], "orders": ["id"]},
          {"added": ["orders"], "dropped": ["legacy"], "modified": [],
           "rename_candidates": [{"from": "aud_old", "to": "aud_new"}]})

    got = handler._timeline(pg.query, cid, 24, None)
    assert got["count"] == 1, got
    assert got["categories"] == ["schema_change"], got
    item = got["items"][0]
    assert item["category"] == "schema_change"
    assert item["source"] == "schema_snapshots"
    assert "app" in item["title"]
    for name in ("orders", "legacy", "aud_old"):
        assert name in item["detail"], item
    assert got["degraded_sources"] == []

    # The relation the old statement named does not exist on a fully migrated
    # cache DB: that is why the category could never be populated.
    assert pg.raw("SELECT to_regclass('schema_changes') IS NULL") == [["t"]]

    # THE OBSERVATION CHANNEL, on the real engine. `_timeline` is the FIFTH
    # interpreter of these rows and the sixth pass swapped its SQL to the shared
    # ALL_ROWS fragment without giving it the channel, so an empty schema_change
    # category read as "no DDL during the incident" over schemas the read never
    # reached. A fully confirmed cluster gets NO sentence, or the banner fires on
    # every timeline and is ignored within a week.
    assert got["observation"]["status"] == "fresh", got["observation"]
    assert got["observation"]["note"] == "", got["observation"]


def test_the_timeline_names_a_schema_it_could_not_confirm(pg):
    """An empty (or non-empty) schema_change category over a cluster with an
    unconfirmable schema is not evidence about that schema. Same shared sentence the
    schema-changes panel and both MCP readers use."""
    cid = "tl-obs-1"
    _snap(pg, cid, "live_s", 2, {"users": ["id"]}, {"added": ["users"]})
    # a schema still serving tables whose stamp is far past the confirmation bar
    _snap(pg, cid, "gone_s", 400 * 24, {"orders": ["id"]}, {},
          last_seen="NOW() - INTERVAL '30 days'")

    got = handler._timeline(pg.query, cid, 24, None)
    obs = got["observation"]
    assert obs["status"] == "not_seen", obs
    assert obs["unconfirmed_schemas"] == ["gone_s"], obs
    assert "gone_s" in obs["note"]
    assert "삭제로 단정하지 않고" in obs["note"]
    for drop_word in ("삭제됨", "dropped"):
        assert drop_word not in obs["note"], drop_word
    # and it is NOT dressed up as a failed read: nothing failed.
    assert got["degraded_sources"] == []


def test_a_refused_dialect_reaches_the_timeline_and_the_panel_as_a_refusal(pg):
    """FINDING 4 on the real engine, at both dashboard surfaces. A MySQL cluster is
    empty BY DECISION: `not_collected` would promise a first baseline on the next ETL
    cycle that is never coming.

    THE ROW IS PRESENT, and that is the whole point of the eighth pass. This test
    used to `DELETE FROM schema_snapshots` for this cluster before driving the
    timeline, which deleted the only row that could exercise the REPLAY path and made
    the cell vacuous: the refusal reached `observation.note` and the replay loop was
    never entered, so a stored DROP replayed as a positive DDL event went unnoticed
    for a whole pass. The pre-refusal collector's rows are exactly what a real MySQL
    cluster has, so they are what this drives.
    """
    cid = "tl-mysql-1"
    # _snap writes cluster_meta with the default PostgreSQL engine (that is what
    # production looks like: the ETL collects meta before the snapshot collector), so
    # the engine is set AFTER it or the ON CONFLICT UPDATE puts postgres back.
    _snap(pg, cid, "appdb", 2, {"users": ["id"]},
          {"added": [], "dropped": ["orders"], "modified": [],
           "rename_candidates": []})
    _meta(pg, cid, engine="aurora-mysql")

    tl = handler._timeline(pg.query, cid, 24, None)
    assert tl["observation"]["status"] == "unsupported_engine", tl["observation"]
    assert tl["degraded_sources"] == [], "a refusal is not a failed read"

    # The item SURVIVES (get_schema_history keeps its rows for the same reason: this
    # is a record, and deleting history is what the contract forbids) and it is
    # LABELLED on the item, where it is rendered, not only in a cluster-level note.
    assert tl["count"] == 1, tl
    item = tl["items"][0]
    assert item["category"] == "schema_change"
    assert handler._TL_DDL_UNSOUND_TAG in item["title"], item["title"]
    assert "판정" in item["detail"], item["detail"]
    # and the underlying record is still legible: labelling is not redaction.
    assert "orders" in item["detail"], item

    # THE PROPERTY THE WHOLE SEQUENCE IS CHASING: the two REPLAY readers agree about
    # this event. get_schema_history hands back the SAME row under `not_supported`
    # (asserted on its own harness in
    # tests/unit/data_pipeline/test_schema_snapshot_real_pg.py, which cannot be
    # imported here: api/dashboard and mcp_servers each ship a `handler.py` and a
    # `schema_diff_util.py`), so a timeline that suppressed it would contradict the
    # tool the agent calls about the same event at the same timestamp.
    panel = handler._schema_changes(pg.query, cid, 7)
    assert panel["ddl_detection"]["status"] == "not_supported", panel["ddl_detection"]
    assert panel["observation"]["status"] == "unsupported_engine"
    assert panel["changes"] == []
    assert "PostgreSQL" in panel["note"] and "pg_namespace" in panel["note"]
    assert "다음 ETL 주기에 최초 baseline" not in panel["note"]
    assert _NEUTRAL not in panel_verdict(panel)

    # FINDING 1 OF THE NINTH PASS, on the engine that produced the report. The
    # HEADLINE used to be `insufficient_history`, whose sentence tells the operator to
    # widen the window or wait for collection to accumulate, and collection is never
    # coming: this engine is refused by decision. The refusal was then appended to the
    # SAME note three sentences later, so the payload carried two answers while the
    # other four interpreters of these rows all said `not_supported` /
    # `unsupported_engine` for this very cluster.
    #   MEASURED here pre-fix, both with this stored row and on a cluster with none:
    #   status "insufficient_history", note = "수집된 이력이 요청 구간을 걸치지 않아
    #   ... 구간을 늘리거나 수집이 쌓일 때까지 기다려야 합니다." + the refusal +
    #   "표시된 내용은 마지막으로 기록된 스냅샷 기준입니다."
    assert panel["status"] == "not_supported", panel["status"]
    for never_coming in ("기다려야", "구간을 늘리거나", "마지막으로 기록된 스냅샷 기준"):
        assert never_coming not in panel["note"], (never_coming, panel["note"])
    assert "기다려도 판정되지 않음" in panel_verdict(panel), panel_verdict(panel)
    # ...and a cluster with NO pre-refusal rows at all reaches the same headline: the
    # refusal is decided from cluster_meta.engine, never from how much is stored.
    bare = "tl-mysql-bare"
    _meta(pg, bare, engine="aurora-mysql")
    bare_panel = handler._schema_changes(pg.query, bare, 7)
    assert bare_panel["status"] == "not_supported", bare_panel
    assert bare_panel["note"] == panel["note"], (bare_panel["note"], panel["note"])


def test_a_refused_dialect_dates_the_row_counts_and_claims_no_ddl_verdict(pg):
    """TENTH PASS, FINDING 2: the last sentence in this payload that asserted a DDL
    verdict exists on an engine that never produces one.

    The ninth pass took the WAIT/WIDEN headline and the phantom snapshot basis off the
    refused dialect, and left the STALE-collection sentence alone with a note saying
    "표시된 현재 행 수와 DDL 판정은 모두 그 시점 기준이며 지금 값이 아닙니다" (it is
    vacuous rather than a promise). It is not vacuous to an operator: it is a fourth
    sentence in the same note saying the displayed DDL 판정 is merely OUT OF DATE, two
    sentences after the refusal said no snapshot is collected for this cluster at all.

    The staleness fact about ROW COUNTS is true and is kept: table_stats IS collected
    here and IS 48h old. Only the DDL half goes.
    """
    cid = "stale-mysql-1"
    _stat(pg, cid, "t", 5000, 10)
    _stat(pg, cid, "t", 90000, 2)  # newest row is 48h old: collection is `stale`
    # AFTER the _stat calls: _stat upserts cluster_meta with the default PostgreSQL
    # engine, so setting the dialect first would be undone by ON CONFLICT UPDATE.
    _meta(pg, cid, engine="aurora-mysql")

    got = handler._schema_changes(pg.query, cid, 7)
    assert got["collection"]["status"] == "stale", got["collection"]
    assert 47.0 <= got["collection"]["age_hours"] <= 49.0, got["collection"]
    assert got["ddl_detection"]["status"] == "not_supported", got["ddl_detection"]
    assert got["observation"]["status"] == "unsupported_engine", got["observation"]

    # THE FACT THAT SURVIVES: the row counts are real, dated, and stale.
    assert "table_stats 수집이" in got["note"], got["note"]
    assert "지금 값이 아닙니다" in got["note"], got["note"]
    # THE FALSEHOOD THAT GOES: no sentence may date a DDL 판정 this engine never made.
    # Measured pre-fix on this exact cell: "... 멈췄습니다 (마지막 수집 ...). 표시된 현재
    # 행 수와 DDL 판정은 모두 그 시점 기준이며 지금 값이 아닙니다."
    assert "DDL 판정" not in got["note"], got["note"]
    # ...and the refusal itself still says why, in the shared composer's words.
    assert "PostgreSQL" in got["note"] and "pg_namespace" in got["note"], got["note"]
    # The other three never-coming sentences the ninth pass removed stay removed here
    # too, on the stale cell they were never driven on.
    for never_coming in ("기다려야", "구간을 늘리거나", "마지막으로 기록된 스냅샷 기준"):
        assert never_coming not in got["note"], (never_coming, got["note"])


def test_a_supported_dialect_still_dates_both_halves(pg):
    """The mutation guard for the sentence above: on PostgreSQL the DDL verdict IS
    dated by table_stats collection, and dropping that clause everywhere would cost
    the operator the fact that an empty DDL diff is only as current as the last
    collection. Same stale cell, supported engine."""
    cid = "stale-pg-1"
    _stat(pg, cid, "t", 5000, 10)
    _stat(pg, cid, "t", 90000, 2)

    got = handler._schema_changes(pg.query, cid, 7)
    assert got["collection"]["status"] == "stale", got["collection"]
    assert "DDL 판정" in got["note"], got["note"]
    assert "지금 값이 아닙니다" in got["note"], got["note"]


def test_timeline_ignores_baseline_and_out_of_window_snapshots(pg):
    """Mutation guard: an empty diff (the baseline row) is not a DDL event, and a
    diff older than the window is not in this window."""
    cid = "tl-2"
    _snap(pg, cid, "app", 1, {"users": ["id"]}, {})
    _snap(pg, cid, "app", 80, {"users": ["id"]}, {"added": ["ancient"]})
    got = handler._timeline(pg.query, cid, 24, None)
    assert got["count"] == 0, got
    assert got["items"] == []
    assert got["degraded_sources"] == []


def test_timeline_names_the_source_it_could_not_read(pg):
    """A missing relation must be VISIBLE in the payload, not a silent empty
    category, and still carry no exception text."""
    def query(sql, params=None):
        if "schema_snapshots" in sql:
            raise RuntimeError(
                'relation "schema_snapshots" does not exist; secret '
                "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:dbops-cache-AbCdEf")
        return pg.query(sql, params)

    got = handler._timeline(query, "tl-1", 24, None)
    assert got["degraded_sources"] == ["schema_change"], got
    blob = json.dumps(got, default=str)
    assert "secretsmanager" not in blob
    assert "does not exist" not in blob
    assert "RuntimeError" not in blob


# ===========================================================================
# 8. THE PANEL AND THIS PAYLOAD ARE THE SAME CONTRACT
# ===========================================================================

_PANEL = _ROOT / "frontend" / "src" / "components" / "dashboard" / "schema-changes-panel.tsx"

# Every field the honesty of this panel depends on. The previous round shipped
# all of them in the payload and the panel read NONE of them, so a
# never-collected cluster and an unchanged schema rendered identically.
_HONESTY_FIELDS = ("status", "note", "ddl_detection", "row_deltas", "collection")


def test_the_payload_still_carries_every_field_the_panel_branches_on(pg):
    """The SERVER half of the contract. The panel half is not a substring search
    for these names: `assert field in src` passes on a comment, a type or a dead
    map key, which is how the whole operator-facing half of the previous round
    could have been deleted with the suite green. It lives in
    tests/unit/api/test_schema_changes_panel_states.py, where the branch a status
    REACHES is parsed, and is mutation-checked there."""
    got = handler._schema_changes(pg.query, "never-collected-1", 7)
    for field in _HONESTY_FIELDS:
        assert field in got, f"{field} missing from the payload"
    assert "감지된 스키마 변경 없음" not in _PANEL.read_text(), (
        "the unqualified empty state is back")


def test_unknown_row_count_is_not_rendered_as_zero(pg):
    """A created/dropped table outside the top 100 has current_rows/baseline_rows
    None. The panel coerced null to 0 and printed "0 행"."""
    cid = "unknown-rows-1"
    pg.raw("DROP SCHEMA IF EXISTS unk CASCADE; CREATE SCHEMA unk")
    pg.raw("CREATE TABLE unk.keeper (id int)")
    api = _DataApi(pg)
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                              "postgres", snapshot_ts=_ago(10))
    pg.raw("CREATE TABLE unk.small_new (id int)")   # never in table_stats
    collect_pg_schema_snapshot(api, _cache_execute(api), "arn:x", "arn:y", cid,
                              "postgres", snapshot_ts=_ago(1 / 24.0))
    _stat(pg, cid, "keeper", 100, 0, schema="unk")  # freshness only

    got = handler._schema_changes(pg.query, cid, 7)
    row = [c for c in got["changes"] if c["table_name"] == "small_new"]
    assert row and row[0]["current_rows"] is None, got["changes"]

    # Not `"행 수 미상" in src`: that passes on the const declaration alone. The
    # null branch of RowCount is sliced out and asserted to be the thing that
    # renders it, and the numeric path asserted NOT to.
    src = _PANEL.read_text()
    body = src[src.index("function RowCount("):src.index("\nfunction ChangeRow(")]
    null_branch = body[body.index("if (value === null) {"):body.index("\n  }")]
    assert "{UNKNOWN_ROWS}" in null_branch, null_branch
    assert "table_stats는 매 주기 상위 100개" in null_branch, "the null branch lost its why"
    numeric = body[body.index("\n  }"):]
    assert "{UNKNOWN_ROWS}" not in numeric
    assert "fmtNumber(value)" in numeric, numeric
    assert 'const UNKNOWN_ROWS = "행 수 미상";' in src


# ===========================================================================
# 9. THE STATE MATRIX, DRIVEN BY THE SHIPPED SQL ON THIS SERVER
# ===========================================================================
# tests/unit/api/test_schema_changes_panel_states.py enumerates the full
# cross-product of the four signals over a fake `query`, because a real engine
# cannot be coerced into `ddl=unavailable` or `ok-with-one-blind-schema` without a
# dozen fixtures. What it CANNOT do is notice that the reader's SQL does not
# parse, or that PostgreSQL resolves the pair differently from the way the fake
# hands rows back. So the cells a real server can produce are produced here, by
# the SHIPPED statements against real rows, and the signal tuple is compared
# against the SAME expectation the enumeration uses.
#
# Rows go in through INSERT rather than through the collector for these cells on
# purpose: the collector is store-on-change and snapshots EVERY visible schema,
# so it cannot be steered into "three snapshots of one schema whose endpoints
# match" without every other schema left behind by an earlier test in this module
# turning up as baseline_only. The producer has its own end-to-end coverage in
# sections 1-7 above; what is under test HERE is the reader's pair resolution and
# the derivation on top of it.

_MX = "mx"  # schema used by the matrix cells


def _mx_setup(pg, cid, snaps, stats):
    # A registered PostgreSQL cluster, whether or not it has produced anything yet:
    # cluster_meta lands on the ETL's FIRST run, before either producer.
    _meta(pg, cid)
    pg.raw(f"DROP SCHEMA IF EXISTS {_MX} CASCADE; CREATE SCHEMA {_MX}")
    for hours_ago, tables in snaps:
        _snap(pg, cid, _MX, hours_ago, {t: ["id"] for t in tables}, {})
    for table, rows, age_days in stats:
        _stat(pg, cid, table, rows, age_days, schema=_MX)


def _signals(p):
    return (p["status"], p["ddl_detection"]["status"], p["row_deltas"]["status"],
            p["collection"]["status"])


# (cell id, snapshots [(hours ago, tables)], table_stats [(table, rows, days ago)],
#  expected signal tuple, phrase the panel must show, phrases it must not)
_H = 24.0
_CELLS = [
    ("never_collected", [], [],
     ("not_collected", "not_collected", "no_data", "no_data"),
     "수집 이력이 없어", [_NEUTRAL]),

    ("snapshots_entirely_outside_the_window",
     [(60 * _H, ["a"]), (40 * _H, ["a", "b"])], [("t", 10, 60), ("t", 20, 40)],
     ("insufficient_history", "outside_window", "insufficient_history", "stale"),
     "비교 가능한 이력이 부족해", [_NEUTRAL]),

    ("snapshots_outside_the_window_but_rows_compare",
     [(60 * _H, ["a"]), (40 * _H, ["a", "b"])], [("t", 1000, 10), ("t", 1000, 0)],
     ("partial", "outside_window", "ok", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),

    # The neighbouring cell to the one above, and the fifth pass's finding: the
    # history STARTS inside the window (a cluster registered 5 days ago, asked
    # about 7), so there is no snapshot at or before the window start and the pair
    # spans less than :days. The diff is real over the span it had, which is why
    # this reached ddl=ok, ddl_complete and `no_changes` before `partial_window`
    # was added to the blindness test.
    ("snapshot_history_starts_inside_the_window",
     [(5 * _H, ["a"]), (3 * _H, ["a", "b"]), (1.0, ["a"])],
     [("t", 1000, 10), ("t", 1000, 0)],
     ("partial", "ok", "ok", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),

    ("stale_collection_with_a_real_change",
     [(10 * _H, ["a"]), (2 * _H, ["a", "b"])], [("t", 1000, 10), ("t", 1000, 2)],
     ("ok", "ok", "ok", "stale"), None, None),

    ("stale_collection_with_no_changes",
     [(10 * _H, ["a"]), (5 * _H, ["a", "b"]), (2 * _H, ["a"])],
     [("t", 1000, 10), ("t", 1000, 2)],
     ("no_changes", "ok", "ok", "stale"), _NEUTRAL, ["일부 신호만 판정됨"]),

    ("fresh_with_no_changes",
     [(10 * _H, ["a"]), (5 * _H, ["a", "b"]), (0.2, ["a"])],
     [("t", 1000, 10), ("t", 1000, 0)],
     ("no_changes", "ok", "ok", "fresh"), _NEUTRAL, ["일부 신호만 판정됨"]),
]


@pytest.mark.parametrize("cell", _CELLS, ids=[c[0] for c in _CELLS])
def test_matrix_cell_on_a_real_engine(pg, cell):
    cid, snaps, stats, expected, phrase, forbidden = cell
    _mx_setup(pg, f"mx-{cid}", snaps, stats)
    got = handler._schema_changes(pg.query, f"mx-{cid}", 7)
    assert _signals(got) == expected, got
    if phrase:
        jsx = panel_verdict(got)
        assert phrase in jsx, (cid, jsx)
        for bad in forbidden or ():
            assert bad not in jsx, (cid, bad)


def test_the_cache_db_without_schema_v26_is_its_own_cell(pg):
    """The one cell whose trigger is a FAILING read, so it needs the wrapper
    rather than rows. Same tuple the enumeration expects."""
    cid = "mx-no-v26"
    _mx_setup(pg, cid, [], [("t", 1000, 10), ("t", 1000, 0)])

    def query(sql, params=None):
        if "schema_snapshots" in sql:
            raise RuntimeError('relation "schema_snapshots" does not exist')
        return pg.query(sql, params)

    got = handler._schema_changes(query, cid, 7)
    assert _signals(got) == ("partial", "unavailable", "ok", "fresh"), got
    assert "일부 신호만 판정됨" in panel_verdict(got)
    assert _NEUTRAL not in panel_verdict(got)
    assert "schema_v26" in got["note"]


def test_a_30_day_question_answered_from_7_days_of_snapshots(pg):
    """FINDING 2 of the fifth pass, in the words the finding used, with the pair
    resolution MEASURED on the server.

    Pre-fix observation on this harness: baseline_outside_window TRUE,
    partial_window_schemas ['mx'], row deltas ok, collection fresh, and
    status "no_changes" with the panel headline "이 구간에서 감지된 변경 없음". A DDL
    change 20 days ago is invisible to that answer, and the headline said the
    window was quiet."""
    cid = "mx-30d-from-7d"
    _mx_setup(pg, cid, [(7 * _H, ["users", "orders"]),
                        (3 * _H, ["users", "orders", "tmp_x"]),
                        (0.5, ["users", "orders"])],
              [("t", 1000, 40), ("t", 1000, 0)])

    rows = pg.query(handler._SCHEMA_SNAPSHOT_PAIRS_SQL,
                    {"cluster_id": cid, "read_scope": _established_scope(pg, cid),
                     "days": "30"})
    assert rows and rows[0]["baseline_outside_window"] is True, rows
    assert rows[0]["baseline_is_latest"] is False, rows[0]
    assert int(rows[0]["snapshots_for_schema"]) == 3, rows[0]

    got = handler._schema_changes(pg.query, cid, 30)
    assert got["ddl_detection"]["status"] == "ok"
    assert got["ddl_detection"]["schemas_compared"] == 1
    assert got["ddl_detection"]["partial_window_schemas"] == [_MX]
    assert got["row_deltas"]["status"] == "ok"
    assert got["collection"]["status"] == "fresh"
    assert got["changes"] == []
    assert got["status"] == "partial", got["status"]
    assert _NEUTRAL not in panel_verdict(got)
    assert "구간만" in got["note"] and _MX in got["note"]

    # THE MUTATION GUARD. A window the history DOES span must still be able to
    # reach a real negative, or "partial" means nothing. Same rows, days=7: the
    # 7-day-old snapshot is at or before the window start, so the pair spans the
    # whole window and the endpoint diff is genuinely empty (tmp_x came and went
    # inside it, which is what the store-on-change endpoint diff means).
    got7 = handler._schema_changes(pg.query, cid, 7)
    assert got7["ddl_detection"]["partial_window_schemas"] == []
    assert got7["status"] == "no_changes", got7["status"]
    assert _NEUTRAL in panel_verdict(got7)


def test_an_unreadable_snapshot_table_is_not_evidence_of_a_new_cluster(pg):
    """`not_collected` and its note claim BOTH sources hold nothing for this
    cluster. When the schema_snapshots read raises, snapshots_stored is 0 for want
    of a read, so that sentence would be a negative the data cannot support.
    Measured before the guard: status "not_collected" with _SC_NO_HISTORY and
    _SC_DDL_UNAVAILABLE contradicting each other in one note."""
    _meta(pg, "mx-unreadable-and-no-stats")

    def query(sql, params=None):
        if "schema_snapshots" in sql:
            raise RuntimeError('relation "schema_snapshots" does not exist')
        return pg.query(sql, params)

    got = handler._schema_changes(query, "mx-unreadable-and-no-stats", 7)
    assert got["ddl_detection"]["status"] == "unavailable"
    assert got["collection"]["status"] == "no_data"
    assert got["row_deltas"]["status"] == "no_data"
    assert got["status"] == "insufficient_history", got["status"]
    assert "모두 이 클러스터 행이 없음" not in got["note"], got["note"]
    assert "schema_v26" in got["note"]
    assert _NEUTRAL not in panel_verdict(got)

    # A cluster that really has nothing, on the same server, still says so.
    clean = handler._schema_changes(pg.query, "mx-unreadable-and-no-stats", 7)
    assert clean["status"] == "not_collected", clean["status"]
    assert "모두 이 클러스터 행이 없음" in clean["note"]


def test_every_matrix_cell_reads_differently_from_every_other(pg):
    """The property three passes over this surface kept losing: two cells that
    render the same thing. Compared as (signal tuple, headline sentence) so a cell
    cannot be distinguished only in a field nobody renders."""
    seen = {}
    cells = list(_CELLS) + [("no_v26", None, None, None, None, None)]
    for cid, snaps, stats, _e, _p, _f in cells:
        if cid == "no_v26":
            _mx_setup(pg, "mx-no-v26", [], [("t", 1000, 10), ("t", 1000, 0)])

            def query(sql, params=None):
                if "schema_snapshots" in sql:
                    raise RuntimeError("no relation")
                return pg.query(sql, params)
            got = handler._schema_changes(query, "mx-no-v26", 7)
        else:
            _mx_setup(pg, f"mx-{cid}", snaps, stats)
            got = handler._schema_changes(pg.query, f"mx-{cid}", 7)
        head = None if got["changes"] else re.search(
            r'<div className="text-(?:zinc-400|amber-300)">(.*?)</div>',
            panel_verdict(got)).group(1).strip()
        key = (_signals(got), head)
        assert key not in seen, f"{cid} is indistinguishable from {seen[key]}: {key}"
        seen[key] = cid
    assert len(seen) == 8


def test_no_changes_is_reachable_against_a_real_server(pg):
    """`no_changes` now requires ddl_complete AND rows ok. A guard whose licensing
    state cannot be reached is the pass-3 defect, so it is DRIVEN here, not read:
    a window that opens and closes on the same table set, with row deltas under
    the threshold and collection fresh."""
    cid = "mx-reachable"
    _mx_setup(pg, cid, [(10 * _H, ["a"]), (5 * _H, ["a", "b"]), (0.2, ["a"])],
              [("t", 1000, 10), ("t", 1000, 0)])
    got = handler._schema_changes(pg.query, cid, 7)
    assert got["status"] == "no_changes", got
    assert got["ddl_detection"]["schemas_compared"] == 1
    assert got["ddl_detection"]["baseline_only_schemas"] == []
    assert got["ddl_detection"]["outside_window_schemas"] == []
    assert got["row_deltas"]["tables_compared"] == 1
    assert _NEUTRAL in panel_verdict(got)
