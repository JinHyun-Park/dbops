"""Scenario runner against a REAL PostgreSQL, then the REAL RCA over what it wrote.

WHY THIS EXISTS. tests/unit/api/test_scenarios.py drives the runner through a fake
database that records statements and answers reads from a dict. That fake accepts any
SQL at all, so it cannot tell a correct INSERT from one naming a column that does not
exist, and every scenario would keep passing while writing nothing in production. The
same shape of blindness has already shipped defects in this repo twice: a fake cache
that ignored SQL made a SQL-only filter unverifiable, and a MagicMock bedrock client
could not observe a server-side parameter rejection.

So this file:
  1. initdb's a throwaway PostgreSQL cluster,
  2. applies the REAL migration files (schema_v26, schema_v27, schema_v28) plus the
     signal tables lifted verbatim out of the production base schema,
  3. runs the REAL api/scenarios handler against it, and
  4. runs the REAL diagnose_root_cause_impl over the rows the handler just wrote.

Step 4 is the one that matters. It is the whole demo chain, and it asserts the thing a
viewer will actually judge: that pressing a scenario button produces an RCA whose
top-ranked cause is that scenario's signal, and not the row announcing the scenario.

Skipped, not faked, when no initdb/pg_ctl/psql is on the machine.
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
_SQL_DIR = _ROOT / "data-pipeline" / "schema_migrator" / "sql"
_BASE_SCHEMA = _ROOT / "data-pipeline" / "sql" / "schema.sql"
_SCENARIOS_DIR = _ROOT / "api" / "scenarios"

sys.path.insert(0, str(_SCENARIOS_DIR))
sys.path.insert(0, str(_ROOT / "mcp-servers"))

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:c")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:s")
os.environ.setdefault("CACHE_DB_NAME", "postgres")
os.environ.setdefault("SCENARIO_CLUSTER_ID", "scenario-pg-1")

_spec = importlib.util.spec_from_file_location(
    "scenarios_handler_realpg", _SCENARIOS_DIR / "handler.py"
)
handler = importlib.util.module_from_spec(_spec)
sys.modules["scenarios_handler_realpg"] = handler
_spec.loader.exec_module(handler)

from mcp_servers.incident.tools.diagnose_root_cause import (  # noqa: E402
    SELF_EVENT_SOURCES,
    diagnose_root_cause_impl,
)

CID = "scenario-pg-1"
# The handler writes its two halves around this window; the RCA must read the same one.
WINDOW = 30

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
    reason="no local PostgreSQL (initdb/pg_ctl/psql), scenario real-engine test skipped",
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
_PGDATA = os.path.join(tempfile.gettempdir(), f"dbops_scenario_pg_{os.getpid()}")


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

# ---------------------------------------------------------------------------
# Schema bootstrap: real migration files, plus the signal tables lifted verbatim
# out of the production base schema. Lifted rather than hand-written, because a
# hand-written column list is a second source of truth and the whole point here
# is to catch the handler disagreeing with the FIRST one.
# ---------------------------------------------------------------------------

def _lift(src: str, table: str) -> str:
    start = src.index(f"CREATE TABLE IF NOT EXISTS {table}")
    return src[start : src.index(");", start) + 2]


def _migrate(pg):
    base = _BASE_SCHEMA.read_text()
    v2 = (_ROOT / "data-pipeline" / "sql" / "schema_v2.sql").read_text()
    # cluster_meta resolves the DIALECT: schema snapshots are PostgreSQL-only, and
    # without this row the schema scenario would be refused for the wrong reason.
    pg.raw(_lift(base, "cluster_meta"))
    # metric_snapshots and query_stats already declare PARTITION BY RANGE in the
    # shipped DDL, and _lift carries that clause through (the first `);` in the text
    # is the one closing the PARTITION BY). Only the DEFAULT partition is missing,
    # and it is the thing that actually stores rows: without it every INSERT fails
    # with "no partition of relation found for row", which the fake database cannot
    # see either.
    for table, key in (("metric_snapshots", "ts"), ("query_stats", "snapshot_time")):
        ddl = _lift(base, table)
        assert f"PARTITION BY RANGE ({key})" in ddl, (
            f"{table} DDL no longer declares its partition key; the lift is wrong"
        )
        pg.raw(ddl)
        pg.raw(f"CREATE TABLE {table}_default PARTITION OF {table} DEFAULT")
    # The collectors' idempotency relies on this unique index, so it ships here too:
    # the handler's ON CONFLICT DO NOTHING is only meaningful against it, and the
    # INJECT_SECOND offset exists precisely to avoid colliding with it.
    pg.raw("CREATE UNIQUE INDEX uix_metric_snapshots ON metric_snapshots "
           "(cluster_id, ts, metric_type, md5(COALESCE(dimensions::text, '{}')))")
    # event_log is phase 2, blocking_locks phase 4. Lifted from whichever file
    # actually declares them: the migration history is not one file, and guessing
    # is how the first two attempts at this fixture failed.
    pg.raw(_lift(v2, "event_log"))
    v4 = (_ROOT / "data-pipeline" / "sql" / "schema_v4.sql").read_text()
    pg.raw(_lift(v4, "blocking_locks"))
    # schema_snapshots (v26) + read_scope/last_seen_at (v27) + scenario_runs (v28),
    # applied in the migrator's own order.
    for name in ("schema_v26.sql", "schema_v27.sql", "schema_v28.sql"):
        pg.raw((_SQL_DIR / name).read_text())
    pg.raw("INSERT INTO cluster_meta (cluster_id, account_id, region, engine) "
           f"VALUES ('{CID}', '123456789012', 'ap-northeast-2', 'aurora-postgresql')")


def _as_data_api(row: dict) -> dict:
    """Make a row look like the RDS Data API's, which is what production hands over.

    row_to_json nests a jsonb column as real JSON, so a caller gets a list or a dict.
    The Data API has no JSON type: it returns the column as a STRING via stringValue.
    Leaving the nested form here would make this test kinder than production, and it
    was: the handler's `json.loads(injected_json)` raised TypeError against a list and
    an except clause turned the whole purge into a silent no-op.
    """
    return {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in row.items()}


def _query(pg):
    """The handler's `query` contract over the real server: name-keyed dict rows
    for SELECT/RETURNING, [] for everything else."""
    def query(sql, params=None):
        bound = _BIND.sub(lambda m: _lit((params or {})[m.group(1)]), sql)
        stripped = bound.strip()
        if stripped.startswith("/*"):
            stripped = stripped.split("*/", 1)[1].strip()
        upper = stripped.upper()
        if upper.startswith("SELECT"):
            rows = pg.raw(f"SELECT row_to_json(_rj) FROM ({stripped}) _rj")
            return [_as_data_api(json.loads(r[0])) for r in rows]
        if "RETURNING" in upper:
            # A data-modifying statement cannot sit in a plain subquery
            # ("syntax error at or near INTO"); PostgreSQL only accepts one inside
            # a CTE. The Data API has no such restriction, which is why the handler
            # writes the statement the way it does.
            rows = pg.raw(
                f"WITH _w AS ({stripped}) SELECT row_to_json(_w) FROM _w"
            )
            return [_as_data_api(json.loads(r[0])) for r in rows]
        pg.raw(stripped)
        return []
    return query


def _cache(pg):
    """CacheClient.execute shape for diagnose_root_cause_impl."""
    class C:
        def execute(self, sql, params=None):
            bound = _BIND.sub(lambda m: _lit((params or {})[m.group(1)]), sql)
            stripped = bound.strip()
            if stripped.startswith("/*"):
                stripped = stripped.split("*/", 1)[1].strip()
            rows = pg.raw(f"SELECT row_to_json(_rj) FROM ({stripped}) _rj")
            out = [
                {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                 for k, v in json.loads(r[0]).items()}
                for r in rows
            ]
            return _Result(out)
    return C()


def _clear_run(pg, run_id):
    """Remove a run AND the signal rows it injected.

    Not `DELETE FROM scenario_runs` alone. The lock now covers a run for
    LOCK_MINUTES, and _purge_old_runs only clears predecessors past it, both on
    purpose: a second scenario inside that window would analyse a window still full
    of the first one's signals. In a test the runs are seconds apart, so without
    clearing the ROWS each case would either be refused by the lock or diagnose its
    predecessor, which is exactly the production defect these tests exist to pin.
    """
    row = pg.raw(f"SELECT injected_json FROM scenario_runs WHERE id = {run_id}")
    manifest = json.loads(row[0][0]) if row and row[0][0] else []
    for entry in manifest:
        table, times = entry["table"], entry.get("times") or []
        if not times:
            continue
        ts_col = handler._PURGEABLE[table]
        stamps = ", ".join(f"'{t}'" for t in times)
        pg.raw(f"DELETE FROM {table} WHERE cluster_id = '{CID}' "
               f"AND {ts_col} IN ({stamps})")
    pg.raw(f"DELETE FROM scenario_runs WHERE id = {run_id}")


def _event(scenario):
    return {
        "requestContext": {
            "http": {"method": "POST", "path": f"/api/scenarios/{scenario}/run"},
            "authorizer": {"jwt": {"claims": {"email": "demo@dbops.dev"}}},
        },
        "pathParameters": {"id": scenario},
    }


@pytest.fixture(scope="module")
def db(pg):
    _migrate(pg)
    return pg


def _run(pg, scenario):
    """Run a scenario with the enqueue stubbed out (DynamoDB is not in scope here)."""
    calls = {}

    def fake_enqueue(cluster_id, rule_id, **kwargs):
        calls.update({"cluster_id": cluster_id, "rule_id": rule_id, **kwargs})
        return f"task-{scenario}"

    original = handler.enqueue_auto_rca
    handler.enqueue_auto_rca = fake_enqueue
    try:
        resp = handler._run(_query(pg), _event(scenario), scenario, CID)
    finally:
        handler.enqueue_auto_rca = original
    return resp, json.loads(resp["body"]), calls


# Scenario -> the candidate category its report must rank first. This is the
# contract the demo page advertises on each card, so a mismatch here is a card
# that lies about what the button does.
_EXPECTED_TOP = {
    "cpu_saturation": "metric_spike",
    "connection_exhaustion": "metric_spike",
    "lock_contention": "blocking",
    "slow_query_surge": "slow_query",
    "schema_change": "schema_change",
    "failover": "event",
}


@pytest.mark.parametrize("scenario", sorted(_EXPECTED_TOP))
def test_every_scenario_writes_rows_a_real_postgres_accepts(db, scenario):
    """Every INSERT the runner issues must be valid against the shipped DDL.

    The fake-database test cannot fail on a wrong column name, a wrong type, or a
    missing partition: it records the statement and returns []. Here psql runs with
    ON_ERROR_STOP=1, so a disagreement with the real schema raises.
    """
    resp, body, calls = _run(db, scenario)
    assert resp["statusCode"] == 202, body
    assert body["signals_written"], "no signals recorded in the manifest"
    assert calls["observed_at"] == body["anchor_at"]
    assert calls["dedupe"] is False

    # The manifest has to describe rows that are actually there, or the purge is
    # deleting nothing and the injected rows never age out.
    manifest = json.loads(
        db.raw(f"SELECT injected_json FROM scenario_runs WHERE id = {body['run_id']}")[0][0]
    )
    for entry in manifest:
        table, times = entry["table"], entry["times"]
        placeholders = ", ".join(f"'{t}'" for t in times)
        ts_col = handler._PURGEABLE[table]
        n = int(db.raw(
            f"SELECT COUNT(*) FROM {table} WHERE cluster_id = '{CID}' "
            f"AND {ts_col} IN ({placeholders})"
        )[0][0])
        assert n > 0, f"{scenario}: manifest names {table} rows that do not exist"
    _clear_run(db, body["run_id"])


@pytest.mark.parametrize("scenario", sorted(_EXPECTED_TOP))
def test_the_real_rca_ranks_the_scenarios_own_signal_first(db, scenario):
    """THE DEMO CHAIN, end to end, with no mocks between the button and the ranking.

    A viewer judges this feature on one thing: press the button, get a report whose
    top cause is the failure you chose. So the assertion is exactly that, against the
    real ranker reading the real rows through real SQL.

    It is also the only place the self-reference defence is actually PROVEN rather
    than asserted about a constant. The bookkeeping row lands AT the anchor, where the
    recency factor peaks, and `event` carries base weight 4.0 against metric_spike's
    2.0: if SELF_EVENT_SOURCES ever stopped covering the runner's source, this fails
    with the announcement ranked first, which is what the one real auto-RCA in this
    deployment's history actually did.
    """
    resp, body, _ = _run(db, scenario)
    assert resp["statusCode"] == 202, body

    res = diagnose_root_cause_impl(
        _cache(db), CID, around_time=body["anchor_at"], window_minutes=WINDOW
    )
    assert res["status"] == "ok", res
    cands = res.get("candidates") or []
    assert cands, f"{scenario}: the RCA found nothing in the rows it just wrote: {res}"

    top = cands[0]
    assert top["category"] == _EXPECTED_TOP[scenario], (
        f"{scenario}: expected a {_EXPECTED_TOP[scenario]} cause on top, got "
        f"{top['category']} ({top.get('summary')}). Full ranking: "
        + "; ".join(f"{c['category']}={round(c['score'], 2)}" for c in cands)
    )

    # Nothing anywhere in the ranking may come from a source that narrates DBOps'
    # own actions, at any rank. Checked across the whole list rather than just the
    # top, because rank 2 in a report a DBA reads is still a claim.
    self_sourced = [
        c for c in cands
        if (c.get("evidence") or {}).get("source") in SELF_EVENT_SOURCES
    ]
    assert not self_sourced, (
        f"{scenario}: the RCA ranked its own instrumentation: {self_sourced}"
    )
    assert not any(
        "시나리오 실행" in (c.get("summary") or "") for c in cands
    ), f"{scenario}: the scenario announcement itself was ranked"

    # And the scoring data the UI now renders has to be there, or /tasks shows an
    # empty "점수 근거" panel for every candidate.
    assert top.get("score_breakdown"), top
    assert top.get("suggested_action"), top
    assert res.get("scoring_weights") and res.get("scoring_note"), res

    _clear_run(db, body["run_id"])


def test_a_new_run_clears_the_previous_scenarios_signals(db):
    """The fix for a report that explains the PREVIOUS button.

    The two tests above clear their own rows, which is right for isolating them and
    wrong for this: it means neither can detect cross-run contamination. So this one
    does the opposite. It runs lock_contention, back-dates it past LOCK_MINUTES (the
    only thing a real presenter does differently is wait), runs cpu_saturation, and
    asserts the second report is about CPU.

    MEASURED before the fix, with the purge cutoff at 90 minutes: the lock rows were
    still inside the 30-minute window and the slow-query and schema reports ranked
    `blocking 5.58` above their own evidence. The purge cutoff is now the lock, so a
    new run starts by clearing every predecessor the lock no longer protects.
    """
    _, first, _ = _run(db, "lock_contention")
    lock_rows = int(db.raw(
        f"SELECT COUNT(*) FROM blocking_locks WHERE cluster_id = '{CID}'")[0][0])
    assert lock_rows > 0, "the first scenario wrote no lock rows to contaminate with"

    # Age the first run out of the lock window. Nothing else is touched: the signal
    # rows keep their original timestamps, so they are still inside the RCA window
    # the second run is about to analyse. That is the contamination.
    db.raw("UPDATE scenario_runs SET started_at = started_at - INTERVAL '10 minutes' "
           f"WHERE id = {first['run_id']}")

    _, second, _ = _run(db, "cpu_saturation")
    assert int(db.raw(
        f"SELECT COUNT(*) FROM blocking_locks WHERE cluster_id = '{CID}'")[0][0]) == 0, (
        "the new run did not purge the previous scenario's lock rows, so its report "
        "will rank blocking above its own CPU spike"
    )
    assert db.raw(
        f"SELECT status FROM scenario_runs WHERE id = {first['run_id']}")[0][0] == "resolved"

    res = diagnose_root_cause_impl(
        _cache(db), CID, around_time=second["anchor_at"], window_minutes=WINDOW
    )
    cands = res.get("candidates") or []
    assert cands and cands[0]["category"] == "metric_spike", (
        "the second report is not about the second scenario: "
        + "; ".join(f"{c['category']}={round(c['score'], 2)}" for c in cands)
    )
    assert not any(c["category"] == "blocking" for c in cands), (
        "the previous scenario's signals are still in the ranking"
    )
    _clear_run(db, second["run_id"])
    db.raw(f"DELETE FROM scenario_runs WHERE id = {first['run_id']}")


def test_the_lock_refuses_a_second_scenario_inside_the_window(db):
    """Negative control for the test above: without the lock, the purge is not enough.

    The purge only clears predecessors PAST the lock. A second scenario inside the
    window must therefore be refused, or it would run against a window containing the
    first one's signals with nothing having cleared them.
    """
    _, first, _ = _run(db, "lock_contention")
    resp = handler._run(_query(db), _event("cpu_saturation"), "cpu_saturation", CID)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 409, body
    assert body["running"]["scenario_id"] == "lock_contention"
    # And it injected nothing, so the first scenario's window is untouched.
    assert int(db.raw(
        f"SELECT COUNT(*) FROM metric_snapshots WHERE cluster_id = '{CID}' "
        "AND metric_type = 'cpu'")[0][0]) == 0
    _clear_run(db, first["run_id"])
