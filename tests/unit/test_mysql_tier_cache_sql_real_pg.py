"""REAL-ENGINE coverage for the cache SQL the MySQL / rds_instance tiers run.

WHY THIS FILE EXISTS. `CacheClient.engine_of()` is the single dispatch point for
explain_plan, get_vacuum_stats and recommend_index, and it swallows every
exception and returns "" on failure, which every caller reads as "not MySQL".
So a wrong identifier in its statement does not raise: it silently reverts all
three tools to their pre-fix wrong answers (PG syntax sent to MySQL, PG
"dead_tuples"/"bloat_pct" labels on InnoDB, recommend_index count:0 read as a
clean bill of health). Every existing test patches `cache.engine_of` with a
MagicMock, so mutating `cluster_meta` -> `cluster_metaZZZ` or `engine` ->
`engineZZZ` left the whole suite green. Same hole covered the four
mysql_health_checks statements.

A mock cannot close that: it hands back the row the assertion wants no matter
what the SQL says. So this file runs the REAL statements against a real
PostgreSQL server, on the REAL cache schema, through a Data-API-shaped adapter,
and asserts on what comes back out. Mutate any table or column name in the
statements below and these tests fail.

E-3 EXTENSION (bottom section). The same hole reopened for every statement the
E-3 tier added: its doubles returned canned rows regardless of the SQL text, so
MEASURED, all 9 of these identifier mutations left the FULL 2615-test suite green
at 1c8c3bf: sys.dm_os_performance_counters, cntr_value/cntr_type, object_name,
CAST(is_dynamic AS INT), the cluster_settings upsert, the metric_snapshots insert,
cluster_meta, cluster_settings and metric_snapshots in mysql_param_fitness. The
four statements that target the CACHE (PostgreSQL) are therefore EXECUTED here,
against the real schema, including the ON CONFLICT targets and the jsonb casts.
The two T-SQL statements cannot run here (no SQL Server in CI); they are pinned by
identifier in their own unit tests and were executed read-only against
dbops-demo-mssql.

ENGINE: PostgreSQL from the local install (verified against 14.18). Skipped, not
faked, when no initdb/pg_ctl/psql is on the machine.
"""

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

_ROOT = Path(__file__).resolve().parents[2]
_SQL_DIR = _ROOT / "data-pipeline" / "schema_migrator" / "sql"
_COLLECTORS = _ROOT / "data-pipeline" / "etl_collector" / "collectors"

# The migration files that create the tables these statements read and write:
# cluster_meta + metric_snapshots (base), table_stats + cluster_settings + the
# default partitions (v4), cluster_health_findings (v6), and cluster_meta's
# engine_mode / serverlessv2_max_acu columns (v9, which mysql_param_fitness
# projects). Named rather than "apply the whole chain" because v21 needs the
# pgvector extension, which a stock local PostgreSQL does not have. If a later
# migration moves one of these tables, this list is the one-line fix and the
# failure ("relation does not exist" / "column does not exist") says exactly that.
_MIGRATIONS = ["schema.sql", "schema_v4.sql", "schema_v6.sql", "schema_v9.sql"]

sys.path.insert(0, str(_ROOT / "mcp-servers"))
sys.path.insert(0, str(_COLLECTORS.parent))

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:1:cluster:fake")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:1:secret:fake")

from collectors.mysql_health_checks import collect_mysql_health_checks  # noqa: E402
from collectors.mysql_param_fitness import collect_mysql_param_fitness  # noqa: E402
from mcp_servers.performance.tools.vacuum_stats import get_vacuum_stats_impl  # noqa: E402
from mcp_servers.shared.cache_client import CacheClient  # noqa: E402

# The two rds_direct_collector modules are loaded by path, not by sys.path: that
# directory has its own mysql_innodb_status.py, and putting it on the path would
# make which copy `collectors.*` resolves to depend on import order.
_RDS_DIRECT = _ROOT / "data-pipeline" / "rds_direct_collector"


def _load_by_path(name, filename):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, _RDS_DIRECT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MSSQL_SETTINGS = _load_by_path("e3_mssql_settings", "mssql_settings.py")
_MSSQL_COUNTERS = _load_by_path("e3_mssql_perf_counters", "mssql_perf_counters.py")

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

def _reserve_port():
    """Ask the OS for an unused port instead of hardcoding one, and HOLD it.

    Measured: with a hardcoded port and a fixed data dir, a second pytest process
    running this module rmtree's the first one's PGDATA and initdb's over it,
    killing the live server mid-test ("server closed the connection
    unexpectedly", then "connection refused" for every test after it). That is
    not hypothetical, it happened twice while another agent was running the suite
    in this repo. Port from the OS + PID in the path makes two runs independent.

    The socket stays OPEN until just before `pg_ctl start`. Closing it here (the
    previous `with socket.socket()`) released the port at IMPORT time, so between
    collection and start nothing held it and two modules in one process could be
    handed the same number. Bound-but-not-listening refuses connections, so the
    hold does not make `_serving()` see a live server, and it is closed before
    PostgreSQL binds.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    return str(s.getsockname()[1]), s


_PORT, _PORT_HOLD = _reserve_port()
_PGDATA = os.path.join(tempfile.gettempdir(), f"dbops_mysql_tier_pg_{os.getpid()}")


def _release_port():
    """Drop the reservation so PostgreSQL can bind. Idempotent."""
    global _PORT_HOLD
    if _PORT_HOLD is not None:
        _PORT_HOLD.close()
        _PORT_HOLD = None


def _serving(timeout=5.0):
    """Is ANYTHING still answering on this fixture's port?

    Asked instead of trusting pg_ctl, because pg_ctl finds the server through
    `postmaster.pid`: once that file is gone the stop reports nothing useful while
    the postmaster keeps serving. Polled rather than probed once, so a backend that
    outlives the postmaster by a moment is not reported as a live server.
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
    ERRORs once got written off as flake in this repo. `ignore_errors=True` after an
    UNCHECKED stop is exactly how that state was reached, and this module was the
    last copy still doing it. It was also the only one that did not stop before
    `initdb`, so it had no setup-time self-heal either.
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

# `:name` binds, but NOT the `::type` cast that follows one: the lookbehind makes
# the second colon of `::` non-matching, so `:ts::timestamptz` binds ts and
# leaves the cast alone.
_BIND = re.compile(r"(?<!:):([a-z_][a-z0-9_]*)")
_SEP = "\x1f"


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
        """Run SQL through psql. Returns (column_names, rows-as-lists). Header on
        (no -t) so the adapter can build real columnMetadata: a name-keyed row is
        exactly what includeResultMetadata buys, and it is what these callers read."""
        out = subprocess.run(
            [_PSQL, "-h", "127.0.0.1", "-p", _PORT, "-U", "dbops", "-d", "postgres",
             "-v", "ON_ERROR_STOP=1", "-A", "-F", _SEP, "-P", "footer=off", "-c", sql],
            capture_output=True, text=True)
        if out.returncode != 0:
            raise AssertionError(f"psql failed: {out.stderr.strip()}\nSQL: {sql}")
        lines = [ln for ln in out.stdout.splitlines() if ln != ""]
        if not lines:
            return [], []
        return lines[0].split(_SEP), [ln.split(_SEP) for ln in lines[1:]]


class _DataApi:
    """boto3 rds-data client shape over the real server. Both readers under test
    (CacheClient.execute and the collectors' _execute) unwrap
    columnMetadata + records, so this is the whole contract.

    FIDELITY LIMIT: psql hands back text, so every cell arrives as stringValue
    where the real Data API would send longValue for a BIGINT. That is fine for
    what this file tests (which identifiers the statements name) and both payload
    builders coerce with float() anyway, but it is why the assertions below cast
    instead of comparing to ints."""

    def __init__(self, server):
        self.s = server
        self.statements = []

    def execute_statement(self, resourceArn=None, secretArn=None, database=None,
                          sql=None, parameters=None, includeResultMetadata=None):
        vals = {}
        for p in (parameters or []):
            vals[p["name"]] = None if "isNull" in p["value"] else list(p["value"].values())[0]
        bound = _BIND.sub(lambda m: _lit(vals[m.group(1)]), sql)
        self.statements.append(bound)
        cols, rows = self.s.raw(bound)
        if not cols or cols[0].startswith(("INSERT", "UPDATE", "DELETE")):
            return {"columnMetadata": [], "records": []}
        return {
            "columnMetadata": [{"name": c} for c in cols],
            "records": [[({"isNull": True} if c == "" else {"stringValue": c}) for c in row]
                        for row in rows],
        }


def _run(argv):
    """check=True + capture_output hides WHY initdb or pg_ctl failed, which turns
    any environment problem into a bare CalledProcessError. Surface the stderr."""
    out = subprocess.run(argv, capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(
            f"{os.path.basename(argv[0])} failed (rc={out.returncode})\n"
            f"stderr: {out.stderr.strip()}\nstdout: {out.stdout.strip()[-2000:]}")


@pytest.fixture(scope="module")
def server():
    _stop_and_remove()
    os.makedirs(_PGDATA, exist_ok=True)
    _run([_INITDB, "-D", _PGDATA, "-U", "dbops", "--auth=trust"])
    _release_port()  # hand the port over to PostgreSQL, last possible moment
    _run([_PGCTL, "-D", _PGDATA,
          "-o", f"-p {_PORT} -k {_PGDATA} -c listen_addresses=127.0.0.1",
          "-l", os.path.join(_PGDATA, "log"), "-w", "start"])
    try:
        s = _Server()
        for fname in _MIGRATIONS:
            s.raw((_SQL_DIR / fname).read_text())
        yield s
    finally:
        _stop_and_remove()


@pytest.fixture
def fresh(server):
    """A clean slate per test, so one test's rows never make another one pass."""
    server.raw("TRUNCATE cluster_meta, table_stats, cluster_settings, "
               "cluster_health_findings, metric_snapshots")
    return server


def _cache(server):
    c = CacheClient()
    c.rds_data = _DataApi(server)
    return c


def _register(server, cluster_id, engine):
    server.raw(
        "INSERT INTO cluster_meta (cluster_id, account_id, region, engine) "
        f"VALUES ({_lit(cluster_id)}, '000000000000', 'ap-northeast-2', {_lit(engine)})")


def _table_stats(server, cluster_id, schema, table, live, free):
    server.raw(
        "INSERT INTO table_stats (cluster_id, snapshot_time, schema_name, table_name, "
        "  n_live_tup, n_dead_tup, total_bytes) "
        f"VALUES ({_lit(cluster_id)}, NOW(), {_lit(schema)}, {_lit(table)}, "
        f"        {live}, {free}, 1000000)")


# ===========================================================================
# engine_of: the dispatch point. If its SQL is wrong it returns "" and all
# three retooled tools silently answer as PostgreSQL.
# ===========================================================================


def test_engine_of_reads_the_engine_string_out_of_the_real_cluster_meta(fresh):
    _register(fresh, "aurora-my-1", "aurora-mysql")
    _register(fresh, "aurora-pg-1", "aurora-postgresql")
    cache = _cache(fresh)

    assert cache.engine_of("aurora-my-1") == "aurora-mysql"
    assert cache.engine_of("aurora-pg-1") == "aurora-postgresql"


def test_engine_of_is_empty_for_a_cluster_with_no_row_not_an_error(fresh):
    # Distinguishes "no such cluster" (legitimately "") from "the statement is
    # broken" (also "", which is the hole this file closes).
    assert _cache(fresh).engine_of("never-registered") == ""


def test_engine_of_hits_the_cache_db_once_per_cluster(fresh):
    _register(fresh, "aurora-my-1", "aurora-mysql")
    cache = _cache(fresh)
    cache.engine_of("aurora-my-1")
    cache.engine_of("aurora-my-1")
    assert len(cache.rds_data.statements) == 1


# ===========================================================================
# The engine string actually reaching the tool that branches on it.
# ===========================================================================


def test_mysql_cluster_gets_innodb_labels_from_a_real_engine_lookup(fresh):
    _register(fresh, "aurora-my-1", "aurora-mysql")
    # 30% of live rows worth of DATA_FREE: over the tool's 25% MySQL bar.
    _table_stats(fresh, "aurora-my-1", "sampledb", "sales", 1_000_000, 300_000)

    out = get_vacuum_stats_impl(_cache(fresh), "aurora-my-1")

    assert out["engine"] == "mysql"
    row = out["tables"][0]
    assert row["table_name"] == "sales"
    assert float(row["fragmentation_pct"]) == 30.0
    # The PG names InnoDB does not have must not appear at all.
    assert "dead_tuples" not in row and "bloat_pct" not in row
    assert out["warnings"] and "OPTIMIZE TABLE" in out["warnings"][0]


def test_pg_cluster_keeps_the_pg_labels_from_the_same_real_lookup(fresh):
    _register(fresh, "aurora-pg-1", "aurora-postgresql")
    _table_stats(fresh, "aurora-pg-1", "public", "orders", 1_000_000, 300_000)

    out = get_vacuum_stats_impl(_cache(fresh), "aurora-pg-1")

    assert out["engine"] == "postgresql"
    row = out["tables"][0]
    assert int(row["dead_tuples"]) == 300000 and float(row["bloat_pct"]) == 30.0
    assert out["warnings"] and "bloat" in out["warnings"][0]


# ===========================================================================
# mysql_health_checks: three statements (table_stats read, cluster_settings
# read, findings INSERT), all executed for real.
# ===========================================================================


def _run_health(server, cluster_id, ts="2026-07-27T00:00:00+00:00"):
    api = _DataApi(server)
    return collect_mysql_health_checks(
        api, "arn:cache", "arn:secret", "dbops", cluster_id, snapshot_ts=ts)


def _findings(server, cluster_id):
    cols, rows = server.raw(
        "SELECT check_type, severity, subject, value_str, snapshot_time, details "
        f"FROM cluster_health_findings WHERE cluster_id = {_lit(cluster_id)} "
        "ORDER BY check_type, subject")
    # strict: a header/row length mismatch means the psql adapter is broken, and
    # silently dropping a column would make the assertions below meaningless.
    return [dict(zip(cols, r, strict=True)) for r in rows]


def test_fragmentation_finding_lands_in_the_real_findings_table(fresh):
    # 30% free space on a table well over the 100k-live-row floor.
    _table_stats(fresh, "my-1", "sampledb", "sales", 1_000_000, 300_000)
    # Under the row floor: must not produce a finding however fragmented.
    _table_stats(fresh, "my-1", "sampledb", "tiny", 1_000, 900)

    summary = _run_health(fresh, "my-1")
    assert summary["tables_examined"] == 2
    assert summary["tables_over_min_rows"] == 1

    rows = _findings(fresh, "my-1")
    assert [r["check_type"] for r in rows] == ["mysql_fragmentation"]
    assert rows[0]["subject"] == "sampledb.sales"
    assert rows[0]["severity"] == "warning"
    assert "30.0%" in rows[0]["value_str"]
    # details is written as `:details::jsonb`; a wrong cast or column would fail
    # the INSERT, and a wrong key would break the panel.
    assert json.loads(rows[0]["details"])["free_rows_est"] == 300000
    # The shared per-run timestamp has to survive the round trip, or the
    # dashboard's MAX(snapshot_time) query shows only the last batch.
    assert rows[0]["snapshot_time"].startswith("2026-07-27")


def test_ordinary_free_list_churn_writes_nothing(fresh):
    # The two live Aurora MySQL demo tables: 11.26% and 9.27%, both normal.
    _table_stats(fresh, "my-1", "sampledb", "products", 963_662, 108_500)
    _table_stats(fresh, "my-1", "sampledb", "sales", 1_284_750, 119_100)

    assert _run_health(fresh, "my-1")["findings_emitted"] == 0
    assert _findings(fresh, "my-1") == []


def test_settings_are_read_from_the_real_cluster_settings_table(fresh):
    fresh.raw(
        "INSERT INTO cluster_settings (cluster_id, name, value) VALUES "
        "('my-1', 'slow_query_log', 'OFF'), "
        "('my-1', 'innodb_flush_log_at_trx_commit', '2')")

    summary = _run_health(fresh, "my-1")
    assert summary["settings_read"] == 2

    subjects = [r["subject"] for r in _findings(fresh, "my-1")]
    assert subjects == ["innodb_flush_log_at_trx_commit", "slow_query_log"]
    assert {r["check_type"] for r in _findings(fresh, "my-1")} == {"setting_misconfigured"}


def test_no_rows_anywhere_emits_nothing_rather_than_a_clean_bill(fresh):
    summary = _run_health(fresh, "my-1")
    assert summary == {"cluster_id": "my-1", "tables_examined": 0,
                       "tables_over_min_rows": 0, "settings_read": 0,
                       "findings_emitted": 0}
    assert _findings(fresh, "my-1") == []


# ===========================================================================
# E-3: the four CACHE statements the rds_instance tier added, EXECUTED.
#
# All four were mutation-blind at 1c8c3bf: renaming their tables, columns,
# aliases, ON CONFLICT targets or the ::jsonb cast left the full suite green,
# because every double answered with canned rows no matter what the SQL said.
# The T-SQL halves (sys.configurations, sys.dm_os_performance_counters) cannot
# run against PostgreSQL and are pinned by identifier in their own unit files;
# both were executed read-only against dbops-demo-mssql.
# ===========================================================================


def _cache_writer(server):
    """The `cache_execute(sql, params)` closure rds_direct_collector builds.

    Same contract as _make_cache_execute in data-pipeline/rds_direct_collector/
    handler.py, except the statement goes to a real server instead of the Data API.
    """
    def cache_execute(sql, params):
        return server.raw(_BIND.sub(lambda m: _lit(params[m.group(1)]), sql))
    return cache_execute


class _TsqlRows:
    """Stands in for the SQL Server side only. The cache side is the real server.

    A SQL Server result set cannot be produced by PostgreSQL, so the T-SQL read is
    the one thing faked here: the rows below are the MEASURED live result sets.
    """

    def __init__(self, rows):
        self.rows = rows

    def execute_statement(self, **kw):
        def field(v):
            return {"longValue": v} if isinstance(v, int) else {"stringValue": v}
        return {"records": [[field(c) for c in row] for row in self.rows]}


# MEASURED on dbops-demo-mssql: (name, configured, running, is_dynamic).
_MSSQL_SETTING_ROWS = [
    ("max server memory (MB)", "1576", "1576", 1),
    ("min server memory (MB)", "0", "16", 1),     # engine-adjusted, is_dynamic=1
    ("user connections", "0", "40", 0),           # needs a restart
]
# MEASURED live (second probe): (object, counter, cntr_value, cntr_type).
_MSSQL_COUNTER_ROWS = [
    ("SQLServer:Buffer Manager", "Buffer cache hit ratio", 104, 537003264),
    ("SQLServer:Buffer Manager", "Buffer cache hit ratio base", 104, 1073939712),
    ("SQLServer:Buffer Manager", "Page life expectancy", 10449, 65792),
    ("SQLServer:General Statistics", "Processes blocked", 0, 65792),
    ("SQLServer:Memory Manager", "Memory Grants Pending", 0, 65792),
    ("SQLServer:Memory Manager", "Target Server Memory (KB)", 496752, 65792),
    ("SQLServer:Memory Manager", "Total Server Memory (KB)", 184488, 65792),
]


def _settings_rows(server, cluster_id):
    cols, rows = server.raw(
        "SELECT name, value, unit FROM cluster_settings "
        f"WHERE cluster_id = {_lit(cluster_id)} ORDER BY name")
    return [dict(zip(cols, r, strict=False)) for r in rows]


def test_mssql_settings_upsert_executes_against_the_real_cluster_settings(fresh):
    """The UPSERT is run for real, so the ON CONFLICT target must match the
    table's actual primary key and `unit` must fit its actual width."""
    collect = _MSSQL_SETTINGS.collect_mssql_settings
    result = collect(_TsqlRows(_MSSQL_SETTING_ROWS), _cache_writer(fresh),
                     "", "", "mssql-1", "master")
    assert result["settings_upserted"] == 3
    assert result["diverging_from_configured"] == 2

    rows = _settings_rows(fresh, "mssql-1")
    assert [r["name"] for r in rows] == [
        "max server memory (MB)", "min server memory (MB)", "user connections"]
    # RUNNING values, which is what a DBA sees in sp_configure.
    assert [r["value"] for r in rows] == ["1576", "16", "40"]
    # cluster_settings.unit is VARCHAR(50): a longer marker would raise here and
    # nowhere else, because only a real column has a width.
    assert rows[0]["unit"] == ""
    assert "재시작" not in rows[1]["unit"]      # is_dynamic=1, engine-adjusted
    assert "재시작" in rows[2]["unit"]          # is_dynamic=0, restart applies it


def test_mssql_settings_upsert_updates_in_place_on_the_real_primary_key(fresh):
    collect = _MSSQL_SETTINGS.collect_mssql_settings
    write = _cache_writer(fresh)
    collect(_TsqlRows(_MSSQL_SETTING_ROWS), write, "", "", "mssql-1", "master")
    changed = [("max server memory (MB)", "1576", "1200", 1)] + _MSSQL_SETTING_ROWS[1:]
    collect(_TsqlRows(changed), write, "", "", "mssql-1", "master")

    rows = _settings_rows(fresh, "mssql-1")
    assert len(rows) == 3, "ON CONFLICT did not update in place"
    assert rows[0]["value"] == "1200"
    assert "설정값 1576" in rows[0]["unit"]


def test_mssql_perf_counter_insert_executes_against_real_metric_snapshots(fresh):
    """INSERT_METRIC runs for real: the column list, the `:dimensions::jsonb`
    cast and ON CONFLICT DO NOTHING all have to be valid against the partitioned
    metric_snapshots table (which needs its DEFAULT partition to accept a row)."""
    result = _MSSQL_COUNTERS.collect_mssql_perf_counters(
        _TsqlRows(_MSSQL_COUNTER_ROWS), _cache_writer(fresh), "", "",
        "mssql-1", "master")
    assert result["skipped"] == {}

    # Read back through the STRICT cluster-level filter every aggregate reader
    # uses. A dimensioned row would not be visible here, so this proves the
    # dashboard and the anomaly baselines will actually see these series.
    cols, rows = fresh.raw(
        "SELECT metric_type, value FROM metric_snapshots "
        "WHERE cluster_id = 'mssql-1' AND dimensions::text = '{}' "
        "ORDER BY metric_type")
    got = {r[0]: float(r[1]) for r in rows}
    assert got == {
        # 104 / 104, NOT the raw numerator 104.
        "mssql_buffer_cache_hit_ratio": 100.0,
        "mssql_memory_grants_pending": 0.0,
        "mssql_page_life_expectancy_sec": 10449.0,
        "mssql_processes_blocked": 0.0,
        # 184488 / 496752
        "mssql_server_memory_used_pct": 37.14,
    }


# --- mysql_param_fitness: cluster_meta / cluster_settings / metric_snapshots ---

def _register_instance(server, cluster_id, engine, instance_class="db.r6g.large"):
    server.raw(
        "INSERT INTO cluster_meta (cluster_id, account_id, region, engine, "
        "  instance_class, engine_mode) "
        f"VALUES ({_lit(cluster_id)}, '000000000000', 'ap-northeast-2', "
        f"        {_lit(engine)}, {_lit(instance_class)}, 'provisioned')")


def _settings(server, cluster_id, **kv):
    for name, value in kv.items():
        server.raw(
            "INSERT INTO cluster_settings (cluster_id, name, value) VALUES "
            f"({_lit(cluster_id)}, {_lit(name)}, {_lit(str(value))})")


def _series(server, cluster_id, metric_type, value, n, dimensions="{}"):
    server.raw(
        "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
        f"SELECT {_lit(cluster_id)}, NOW() - (g || ' minutes')::interval, "
        f"       {_lit(metric_type)}, {value}, {_lit(dimensions)}::jsonb "
        f"FROM generate_series(1, {n}) g")


def _fitness(server, cluster_id):
    api = _DataApi(server)
    result = collect_mysql_param_fitness(
        api, "arn:cache", "arn:secret", "dbops", cluster_id,
        snapshot_ts="2026-07-29T00:00:00+00:00")
    result["_findings"] = _findings(server, cluster_id)
    return result


def _quiet_workload(server, cluster_id):
    """Enough real rows for M3 to be decidable while M1 and M2 stay silent."""
    _settings(server, cluster_id, max_connections=200,
              sort_buffer_size=262144, innodb_buffer_pool_size=134217728)
    _series(server, cluster_id, "db_connections", 80, 25)


def test_param_fitness_reads_the_real_cluster_meta_projection(fresh):
    """instance_class / engine_mode / serverlessv2_max_acu / engine are read in
    ONE statement; a renamed column silently makes every rule skip."""
    _register_instance(fresh, "my-1", "aurora-mysql")
    _quiet_workload(fresh, "my-1")
    result = _fitness(fresh, "my-1")
    assert result["instance_class"] == "db.r6g.large"
    assert result["instance_memory_gb"] == 16          # mapped, so M2 is live
    assert result["max_connections"] == 200            # read out of cluster_settings
    assert result["peak_connections_7d"] == 80         # read out of metric_snapshots


def test_param_fitness_m3_uses_the_innodb_metric_for_a_standalone_instance(fresh):
    """The rds_instance family writes only innodb_buffer_pool_hit_rate. This runs
    the real 4-column CASE query against real rows of that metric_type."""
    _register_instance(fresh, "rds-1", "mysql")
    _quiet_workload(fresh, "rds-1")
    _series(fresh, "rds-1", "innodb_buffer_pool_hit_rate", 80.0, 25)

    result = _fitness(fresh, "rds-1")
    (row,) = [r for r in result["_findings"] if r["check_type"] == "param_buffer_cache_hit"]
    assert "80.0%" in row["value_str"]
    assert json.loads(row["details"])["metric_type"] == "innodb_buffer_pool_hit_rate"
    assert json.loads(row["details"])["samples"] == 25


def test_param_fitness_m3_prefers_cloudwatch_and_never_averages_the_two(fresh):
    """Aurora has BOTH metric_types in the same table. The CW value must come
    back verbatim: 90.0, not the InnoDB 60.0 and not their 75.0 mean."""
    _register_instance(fresh, "my-1", "aurora-mysql")
    _quiet_workload(fresh, "my-1")
    _series(fresh, "my-1", "buffer_cache_hit", 90.0, 25)
    _series(fresh, "my-1", "innodb_buffer_pool_hit_rate", 60.0, 25)

    result = _fitness(fresh, "my-1")
    (row,) = [r for r in result["_findings"] if r["check_type"] == "param_buffer_cache_hit"]
    assert "90.0%" in row["value_str"]
    assert json.loads(row["details"])["metric_type"] == "buffer_cache_hit"


def test_param_fitness_m3_stays_silent_on_aurora_with_only_the_innodb_series(fresh):
    """The E-3 fallback is gated to rds_instance. Aurora writes
    innodb_buffer_pool_hit_rate too, so an ungated fallback fires this rule on
    Aurora clusters where it used to be silent: a widening, not a no-op."""
    _register_instance(fresh, "my-1", "aurora-mysql")
    _quiet_workload(fresh, "my-1")
    _series(fresh, "my-1", "innodb_buffer_pool_hit_rate", 80.0, 25)

    result = _fitness(fresh, "my-1")
    assert [r for r in result["_findings"]
            if r["check_type"] == "param_buffer_cache_hit"] == []


def test_param_fitness_ignores_per_instance_dimensioned_rows(fresh):
    """Both metric_snapshots reads carry the strict `dimensions::text = '{}'`
    filter. Without it a cluster-level average silently mixes in the per-instance
    rows, which is the recurring defect this repo has paid for repeatedly. A jsonb
    column is the only place that can be checked."""
    _register_instance(fresh, "rds-1", "mysql")
    _settings(fresh, "rds-1", max_connections=200, sort_buffer_size=262144,
              innodb_buffer_pool_size=134217728)
    # Cluster-level: a healthy 99%, under-sampled on purpose (5 rows).
    _series(fresh, "rds-1", "innodb_buffer_pool_hit_rate", 99.0, 5)
    # Per-instance rows: 40 samples at a terrible 10%. If the filter is dropped,
    # the average collapses and the sample count clears MIN_SAMPLES, so the rule
    # fires with a number that describes nothing.
    _series(fresh, "rds-1", "innodb_buffer_pool_hit_rate", 10.0, 40,
            dimensions='{"instance": "rds-1-instance-1"}')
    _series(fresh, "rds-1", "db_connections", 80, 25)
    _series(fresh, "rds-1", "db_connections", 5000, 40,
            dimensions='{"instance": "rds-1-instance-1"}')

    result = _fitness(fresh, "rds-1")
    assert result["peak_connections_7d"] == 80, "dimensioned rows leaked into the peak"
    assert [r for r in result["_findings"]
            if r["check_type"] == "param_buffer_cache_hit"] == []
