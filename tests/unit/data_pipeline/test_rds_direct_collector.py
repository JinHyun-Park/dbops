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


def test_handler_filters_rds_instance_mysql_with_secret():
    h = _load("handler")
    rows = [
        {"cluster_id": "a", "engine_family": "rds_instance", "engine": "mysql",
         "db_secret_arn": "arn:x", "endpoint": "h", "port": 3306},
        {"cluster_id": "b", "engine_family": "rds_instance", "engine": "sqlserver-ex",
         "db_secret_arn": "arn:y", "endpoint": "h", "port": 1433},   # R-4, skip
        {"cluster_id": "c", "engine_family": "rds_instance", "engine": "mysql",
         "db_secret_arn": "", "endpoint": "h", "port": 3306},        # no secret, skip
        {"cluster_id": "d", "engine_family": "relational", "engine": "aurora-mysql"},
    ]
    assert [r["cluster_id"] for r in h._eligible(rows)] == ["a"]


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
