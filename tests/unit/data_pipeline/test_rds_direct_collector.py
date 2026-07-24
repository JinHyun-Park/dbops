"""rds_direct_collector — unit tests.

Three concerns:
  1. The 5 vendored mysql_* collectors must stay byte-identical to the
     etl_collector/collectors originals (parity test).
  2. MySQLDataApiAdapter re-encodes pymysql rows into the RDS-Data field-dict
     shape the vendored collectors' _str/_long/_double helpers unwrap.
  3. handler._eligible filtering + _process_cluster fail-closed isolation.

The handler imports the vendored collectors + adapter as flat modules (the
Lambda asset root is the package dir), so _load puts that dir on sys.path
while executing the module and restores it after (hygiene). pymysql is never
imported by the adapter and only lazily inside handler._connect, so these
tests run without pymysql installed.
"""

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline"

_VENDORED = ["mysql_query_stats.py", "mysql_activity.py", "mysql_locks.py",
             "mysql_innodb_status.py", "mysql_table_stats.py"]


def test_vendored_collectors_are_verbatim_identical():
    for name in _VENDORED:
        src = (_ROOT / "etl_collector" / "collectors" / name).read_text()
        cpy = (_ROOT / "rds_direct_collector" / name).read_text()
        assert src == cpy, f"{name} diverged from etl_collector/collectors copy"


def _load(mod):
    pkg = str(_ROOT / "rds_direct_collector")
    added = pkg not in sys.path
    if added:
        sys.path.insert(0, pkg)
    try:
        spec = importlib.util.spec_from_file_location(
            mod, _ROOT / "rds_direct_collector" / f"{mod}.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        if added and pkg in sys.path:
            sys.path.remove(pkg)


def test_adapter_maps_python_types_to_data_api_fields():
    ad = _load("mysql_adapter")
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        ("abc", 42, 3.14, None, b"bin", datetime(2026, 7, 22, 1, 2, 3)),
    ]
    adapter = ad.MySQLDataApiAdapter(conn)
    out = adapter.execute_statement(sql="SELECT 1")
    row = out["records"][0]
    assert row[0] == {"stringValue": "abc"}
    assert row[1] == {"longValue": 42}
    assert row[2] == {"doubleValue": 3.14}
    assert row[3] == {"isNull": True}
    assert row[4] == {"stringValue": "bin"}          # bytes → utf-8 (errors=replace)
    assert row[5] == {"stringValue": "2026-07-22 01:02:03"}  # datetime → str


def test_adapter_decimal_maps_to_double():
    from decimal import Decimal
    ad = _load("mysql_adapter")
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [(Decimal("7.5"),)]
    out = ad.MySQLDataApiAdapter(conn).execute_statement(sql="SELECT 1")
    assert out["records"][0][0] == {"doubleValue": 7.5}


def test_query_stats_sql_divides_timer_wait_picoseconds_by_1e9():
    # performance_schema TIMER columns are picoseconds (MySQL docs); dividing
    # by 1e6 yields microseconds mislabeled as ms (1000x inflation).
    m = _load("mysql_query_stats")
    assert "SUM_TIMER_WAIT/1000000000" in m.QUERY_STATS_SQL
    assert "AVG_TIMER_WAIT/1000000000" in m.QUERY_STATS_SQL


def test_collect_mysql_query_stats_stores_correct_ms_from_picoseconds():
    # 5_000_000_000 ps == 5.0 ms. The Data API record's doubleValue is what
    # the (corrected) SQL's ROUND(TIMER_WAIT/1e9, 2) would return.
    m = _load("mysql_query_stats")
    client = MagicMock()
    client.execute_statement.return_value = {"records": [[
        {"stringValue": "digest1"},
        {"stringValue": "SELECT 1"},
        {"longValue": 10},
        {"doubleValue": 5.0},
        {"doubleValue": 5.0},
        {"longValue": 100},
    ]]}
    captured = {}
    m.collect_mysql_query_stats(
        client, lambda sql, params: captured.update(params),
        "arn:cluster", "arn:secret", "c1", "db")
    assert captured["total_time_ms"] == 5.0
    assert captured["mean_time_ms"] == 5.0


def test_table_stats_long_tolerates_decimal_shapes():
    m = _load("mysql_table_stats")
    assert m._long({"longValue": 5}) == 5
    assert m._long({"doubleValue": 7.0}) == 7
    assert m._long({"stringValue": "9"}) == 9
    assert m._long({"isNull": True}) == 0


def test_handler_filters_rds_instance_mysql_and_sqlserver_with_secret():
    h = _load("handler")
    rows = [
        {"cluster_id": "a", "engine_family": "rds_instance", "engine": "mysql",
         "db_secret_arn": "arn:x", "endpoint": "h", "port": 3306},
        {"cluster_id": "b", "engine_family": "rds_instance", "engine": "sqlserver-ex",
         "db_secret_arn": "arn:y", "endpoint": "h", "port": 1433},   # R-4: now eligible
        {"cluster_id": "c", "engine_family": "rds_instance", "engine": "mysql",
         "db_secret_arn": "", "endpoint": "h", "port": 3306},        # no secret, skip
        {"cluster_id": "e", "engine_family": "rds_instance", "engine": "postgres",
         "db_secret_arn": "arn:z", "endpoint": "h", "port": 5432},   # neither engine, skip
        {"cluster_id": "d", "engine_family": "relational", "engine": "aurora-mysql"},
    ]
    assert [r["cluster_id"] for r in h._eligible(rows)] == ["a", "b"]


def test_process_cluster_never_raises_and_isolates_failures():
    h = _load("handler")
    # Real secret shape so json.loads succeeds — otherwise the parse dies first
    # and the injected connect failure below is never reached (dead path).
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": json.dumps({"username": "u", "password": "p"})}
    called = {"n": 0}

    def _boom(**kw):
        called["n"] += 1
        raise RuntimeError("boom")

    h._CONNECT_FACTORY = _boom
    res = h._process_cluster(
        {"cluster_id": "a", "endpoint": "h", "port": 3306, "db_secret_arn": "arn:x"},
        secrets=secrets, cache_execute=lambda *a, **k: None, run_ts="2026-07-22T00:00:00+00:00")
    assert res["cluster_id"] == "a" and "error" in res   # never raises, returns error marker
    assert called["n"] == 1                               # the real connect path was reached


# --- R-4: SQL Server DMV collectors --------------------------------------


def test_mssql_adapter_maps_python_types_positionally():
    ad = _load("mssql_adapter")
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        ("abc", 42, 3.14, None, b"bin", datetime(2026, 7, 22, 1, 2, 3)),
    ]
    out = ad.MSSQLDataApiAdapter(conn).execute_statement(sql="SELECT 1")
    row = out["records"][0]
    assert row[0] == {"stringValue": "abc"}
    assert row[1] == {"longValue": 42}
    assert row[2] == {"doubleValue": 3.14}
    assert row[3] == {"isNull": True}
    assert row[4] == {"stringValue": "bin"}
    assert row[5] == {"stringValue": "2026-07-22 01:02:03"}


def test_mssql_adapter_decimal_maps_to_double_and_rejects_params():
    from decimal import Decimal
    ad = _load("mssql_adapter")
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [(Decimal("7.5"),)]
    adapter = ad.MSSQLDataApiAdapter(conn)
    assert adapter.execute_statement(sql="SELECT 1")["records"][0][0] == {"doubleValue": 7.5}
    import pytest
    with pytest.raises(ValueError):
        adapter.execute_statement(sql="SELECT 1", parameters=[{"x": 1}])


def test_mssql_query_stats_sql_has_top100_and_microsecond_conversion():
    # dm_exec_query_stats.total_elapsed_time is MICROSECONDS → /1000.0 = ms.
    m = _load("mssql_query_stats")
    assert "TOP 100" in m.QUERY_STATS_SQL
    assert "SUM(qs.total_elapsed_time)/1000.0" in m.QUERY_STATS_SQL
    assert "INSERT INTO query_stats" in m.INSERT_SQL


def test_mssql_query_stats_sql_aggregates_one_row_per_query_hash():
    # dm_exec_query_stats is per-cached-plan, so one query_hash can span several
    # rows in a single snapshot. query_regression PARTITIONs BY query_hash and
    # LAGs by snapshot_time — duplicate same-tick hashes corrupt the per-interval
    # delta. GROUP BY query_hash collapses plans to one row/hash (MySQL/PG shape).
    m = _load("mssql_query_stats")
    assert "GROUP BY qs.query_hash" in m.QUERY_STATS_SQL
    # counts must be SUMmed across plans, not taken from a single plan row.
    assert "SUM(qs.execution_count)" in m.QUERY_STATS_SQL
    assert "SUM(qs.total_rows)" in m.QUERY_STATS_SQL
    # ordering/TOP N is over the aggregated total, not a per-plan value.
    assert "ORDER BY SUM(qs.total_elapsed_time) DESC" in m.QUERY_STATS_SQL


def test_mssql_query_stats_stores_ms_from_microseconds():
    # 5_000_000 µs / 1000.0 == 5000.0 ms. The doubleValue is what the SQL's
    # total_elapsed_time/1000.0 returns for a 5s statement.
    m = _load("mssql_query_stats")
    client = MagicMock()
    client.execute_statement.return_value = {"records": [[
        {"stringValue": "0xABCD"},
        {"stringValue": "SELECT 1"},
        {"longValue": 10},
        {"doubleValue": 5000.0},     # 5_000_000 µs / 1000.0
        {"doubleValue": 500.0},      # mean
        {"longValue": 100},
    ]]}
    captured = {}
    m.collect_mssql_query_stats(
        client, lambda sql, params: captured.update(params),
        "", "", "c1", "db")
    assert captured["total_time_ms"] == 5000.0
    assert captured["mean_time_ms"] == 500.0
    assert captured["query_hash"] == "0xABCD"
    assert captured["calls"] == 10
    assert captured["rows_returned"] == 100
    # marker present on the target read
    assert "/* source=dbops-etl */" in client.execute_statement.call_args.kwargs["sql"]


def test_mssql_query_stats_emitted_rows_unique_by_query_hash():
    # Given a snapshot with distinct hashes (the GROUP BY guarantee), every row
    # inserted into query_stats carries a distinct query_hash and the ms units
    # from the SQL's /1000.0 pass through untouched.
    m = _load("mssql_query_stats")
    client = MagicMock()
    client.execute_statement.return_value = {"records": [
        [{"stringValue": "0xAAAA"}, {"stringValue": "SELECT a"},
         {"longValue": 30}, {"doubleValue": 900.0}, {"doubleValue": 30.0}, {"longValue": 300}],
        [{"stringValue": "0xBBBB"}, {"stringValue": "SELECT b"},
         {"longValue": 10}, {"doubleValue": 200.0}, {"doubleValue": 20.0}, {"longValue": 50}],
    ]}
    seen = []
    m.collect_mssql_query_stats(
        client, lambda sql, params: seen.append(dict(params)),
        "", "", "c1", "db")
    hashes = [p["query_hash"] for p in seen]
    assert len(hashes) == len(set(hashes)) == 2   # no duplicate hash in one collection
    assert seen[0]["total_time_ms"] == 900.0 and seen[0]["calls"] == 30   # units/counts preserved


def test_mssql_activity_maps_state_long_running_and_blocking():
    m = _load("mssql_activity")
    client = MagicMock()
    # 3 sequential target reads: state breakdown, long-running, blocking.
    client.execute_statement.side_effect = [
        {"records": [
            [{"stringValue": "running"}, {"longValue": 3}],
            [{"stringValue": "sleeping"}, {"longValue": 7}],
        ]},
        {"records": [[
            {"longValue": 55},                 # pid (session_id)
            {"stringValue": "app_user"},       # username
            {"stringValue": "suspended"},      # state
            {"doubleValue": 12.0},             # duration_sec (12000ms/1000.0)
            {"stringValue": "SELECT big"},     # query_text
            {"stringValue": "LCK_M_X"},        # wait_event_type
            {"stringValue": "host1"},          # client_addr
        ]]},
        {"records": [[
            {"longValue": 60},                 # blocked_pid
            {"stringValue": "victim"},         # blocked_user
            {"longValue": 55},                 # blocking_pid
            {"stringValue": "hog"},            # blocking_user
            {"stringValue": "UPDATE t"},       # blocked_query
            {"stringValue": "SELECT big"},     # blocking_query
            {"stringValue": "LCK_M_X"},        # locktype
            {"stringValue": "KEY: 5:72"},      # relation
            {"doubleValue": 8.0},              # blocked_duration_sec
        ]]},
    ]
    inserts = []
    m.collect_mssql_activity(
        client, lambda sql, params: inserts.append((sql, params)),
        "", "", "c1", "db")

    metric_rows = [p for s, p in inserts if "metric_snapshots" in s]
    assert {"conn_active": 3.0}.items() <= {r["metric_type"]: r["value"] for r in metric_rows}.items()
    assert {r["metric_type"]: r["value"] for r in metric_rows}["conn_idle"] == 7.0

    long_rows = [p for s, p in inserts if "long_running_queries" in s]
    assert len(long_rows) == 1
    assert long_rows[0]["pid"] == 55 and long_rows[0]["duration_sec"] == 12.0
    assert long_rows[0]["username"] == "app_user"

    block_rows = [p for s, p in inserts if "blocking_locks" in s]
    assert len(block_rows) == 1
    assert block_rows[0]["blocked_pid"] == 60 and block_rows[0]["blocking_pid"] == 55


def test_mssql_activity_long_running_sql_thresholds_and_ms_conversion():
    # dm_exec_requests.total_elapsed_time is MILLISECONDS → /1000.0 = sec.
    m = _load("mssql_activity")
    assert "> 5000" in m.LONG_RUNNING_SQL
    assert "/1000.0" in m.LONG_RUNNING_SQL and "AS duration_sec" in m.LONG_RUNNING_SQL
    assert "blocking_session_id <> 0" in m.BLOCKING_SQL
    for sql in (m.ACTIVITY_SQL, m.LONG_RUNNING_SQL, m.BLOCKING_SQL):
        assert "sys.dm_exec" in sql


def test_mssql_waits_sql_and_dimensioned_metric():
    m = _load("mssql_waits")
    assert "TOP 20" in m.WAITS_SQL
    assert "sys.dm_os_wait_stats" in m.WAITS_SQL
    assert "LAZYWRITER_SLEEP" in m.WAITS_SQL          # benign idle waits excluded
    client = MagicMock()
    client.execute_statement.return_value = {"records": [[
        {"stringValue": "PAGEIOLATCH_SH"},
        {"longValue": 12345},
        {"longValue": 42},
    ]]}
    inserts = []
    m.collect_mssql_waits(
        client, lambda sql, params: inserts.append((sql, params)),
        "", "", "c1", "db")
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "metric_snapshots" in sql
    assert params["metric_type"] == "mssql_wait_ms"
    assert params["value"] == 12345.0
    assert '"wait_type": "PAGEIOLATCH_SH"' in params["dimensions"]


def test_process_cluster_dispatches_sqlserver_to_mssql_collectors():
    h = _load("handler")
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {
        "SecretString": json.dumps({"username": "u", "password": "p"})}

    mysql_calls = {"n": 0}

    def _mysql_boom(**kw):
        mysql_calls["n"] += 1
        raise RuntimeError("mysql factory must not be reached for sqlserver")

    h._CONNECT_FACTORY = _mysql_boom

    mssql_calls = {"n": 0}

    def _fake_mssql(**kw):
        mssql_calls["n"] += 1
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []   # empty DMVs → collectors succeed, no cache writes
        return conn

    h._MSSQL_CONNECT_FACTORY = _fake_mssql

    res = h._process_cluster(
        {"cluster_id": "sql1", "engine": "sqlserver-ex", "engine_family": "rds_instance",
         "endpoint": "h", "port": 1433, "db_secret_arn": "arn:x"},
        secrets=secrets, cache_execute=lambda *a, **k: None,
        run_ts="2026-07-22T00:00:00+00:00")

    assert mysql_calls["n"] == 0                 # mysql collectors NOT called
    assert mssql_calls["n"] == 1                 # pytds factory used exactly once
    assert "error" not in res
    assert set(res["collected"]) == {"query_stats", "activity", "waits"}
