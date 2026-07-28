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

ENGINE: PostgreSQL from the local install. The production DDL in
data-pipeline/schema_migrator/sql/ is applied in the migrator's own numeric
order. Skipped, not faked, when no initdb/pg_ctl/psql is on the machine.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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

# Own port + own data dir: the schema_snapshot real-engine test runs its own
# postmaster on 55433 and both fixtures rmtree what they created.
_PORT = "55437"
_PGDATA = os.path.join(tempfile.gettempdir(), "dbops_schema_changes_pg")

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
    shutil.rmtree(_PGDATA, ignore_errors=True)
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
        subprocess.run([_PGCTL, "-D", _PGDATA, "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(_PGDATA, ignore_errors=True)


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


def _stat(pg, cluster, table, rows, age_days, schema="app", bytes_=1000):
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
    # A real negative for what IS measurable, so not "not_collected" overall.
    assert got["status"] == "no_changes"
    assert got["changes"] == []
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
