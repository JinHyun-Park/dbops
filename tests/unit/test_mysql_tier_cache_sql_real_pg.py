"""REAL-ENGINE coverage for the cache SQL the Aurora MySQL tier dispatches on.

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
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SQL_DIR = _ROOT / "data-pipeline" / "schema_migrator" / "sql"
_COLLECTORS = _ROOT / "data-pipeline" / "etl_collector" / "collectors"

# The three migration files that create the four tables these statements read
# and write: cluster_meta (base), table_stats + cluster_settings (v4),
# cluster_health_findings (v6). Named rather than "apply the whole chain"
# because v21 needs the pgvector extension, which a stock local PostgreSQL does
# not have. If a later migration moves one of these tables, this list is the
# one-line fix and the failure ("relation does not exist") says exactly that.
_MIGRATIONS = ["schema.sql", "schema_v4.sql", "schema_v6.sql"]

sys.path.insert(0, str(_ROOT / "mcp-servers"))
sys.path.insert(0, str(_COLLECTORS.parent))

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:1:cluster:fake")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:1:secret:fake")

from collectors.mysql_health_checks import collect_mysql_health_checks  # noqa: E402
from mcp_servers.performance.tools.vacuum_stats import get_vacuum_stats_impl  # noqa: E402
from mcp_servers.shared.cache_client import CacheClient  # noqa: E402

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
    """Ask the OS for an unused port instead of hardcoding one.

    Measured: with a hardcoded port and a fixed data dir, a second pytest process
    running this module rmtree's the first one's PGDATA and initdb's over it,
    killing the live server mid-test ("server closed the connection
    unexpectedly", then "connection refused" for every test after it). That is
    not hypothetical, it happened twice while another agent was running the suite
    in this repo. Port from the OS + PID in the path makes two runs independent."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


_PORT = _free_port()
_PGDATA = os.path.join(tempfile.gettempdir(), f"dbops_mysql_tier_pg_{os.getpid()}")

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
    shutil.rmtree(_PGDATA, ignore_errors=True)
    os.makedirs(_PGDATA, exist_ok=True)
    _run([_INITDB, "-D", _PGDATA, "-U", "dbops", "--auth=trust"])
    _run([_PGCTL, "-D", _PGDATA,
          "-o", f"-p {_PORT} -k {_PGDATA} -c listen_addresses=127.0.0.1",
          "-l", os.path.join(_PGDATA, "log"), "-w", "start"])
    try:
        s = _Server()
        for fname in _MIGRATIONS:
            s.raw((_SQL_DIR / fname).read_text())
        yield s
    finally:
        subprocess.run([_PGCTL, "-D", _PGDATA, "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(_PGDATA, ignore_errors=True)


@pytest.fixture
def fresh(server):
    """A clean slate per test, so one test's rows never make another one pass."""
    server.raw("TRUNCATE cluster_meta, table_stats, cluster_settings, cluster_health_findings")
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
