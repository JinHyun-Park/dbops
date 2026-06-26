"""PG engine-internals + MySQL InnoDB-status collectors → metric_snapshots + findings."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[3] / "data-pipeline/etl_collector/collectors"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _C / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pg = _load("pg_engine_internals")
my = _load("mysql_innodb_status")


def _capture():
    """A fake cache_execute that records (table, params) per call."""
    calls = []

    def cache_execute(sql, params):
        table = "finding" if "cluster_health_findings" in sql else (
            "metric" if "metric_snapshots" in sql else "other")
        calls.append((table, params))

    return calls, cache_execute


# ── MySQL InnoDB status parsing (pure) ───────────────────────────────────────

_BLOB = """
------------
TRANSACTIONS
------------
Trx id counter 12345
History list length 2500000
---
LOG
---
Log sequence number 1100000000
Last checkpoint at 1090000000
--------
FILE I/O
--------
I/O thread 0 state: waiting for i/o request
Pending normal aio reads: [1] , aio writes: [0, 1, 0, 0] ,
Pending flushes (fsync) log: 1; buffer pool: 2
----------------------
BUFFER POOL AND MEMORY
----------------------
Buffer pool hit rate 998 / 1000, young-making rate 0 / 1000
--------------
ROW OPERATIONS
--------------
Number of rows inserted 100, updated 0, deleted 0, read 100
1.00 inserts/s, 2.00 updates/s, 0.00 deletes/s, 3.00 reads/s
"""


def test_parse_innodb_status_extracts_all_fields():
    m = my.parse_innodb_status(_BLOB)
    assert m["innodb_history_list_length"] == 2500000.0
    assert m["innodb_buffer_pool_hit_rate"] == 99.8  # 998 / 1000 → %
    assert abs(m["innodb_checkpoint_age_mb"] - (10_000_000 / 1048576.0)) < 1e-6
    assert m["innodb_pending_io"] == 5.0  # aio [1] + [0,1,0,0] + fsync (1 + 2)
    assert m["innodb_row_ops_per_sec"] == 6.0  # 1 + 2 + 0 + 3 (ins/upd/del/read /s)


def test_parse_innodb_status_partial_blob_skips_missing():
    m = my.parse_innodb_status("History list length 42\n")
    assert m == {"innodb_history_list_length": 42.0}  # others absent → omitted


def test_parse_innodb_status_empty_is_safe():
    assert my.parse_innodb_status("") == {}
    assert my.parse_innodb_status(None) == {}


def test_collect_mysql_innodb_status_inserts_metrics_and_hll_finding():
    rds = MagicMock()
    rds.execute_statement.return_value = {
        "records": [[{"stringValue": "InnoDB"}, {"stringValue": ""}, {"stringValue": _BLOB}]]
    }
    calls, cache_execute = _capture()
    res = my.collect_mysql_innodb_status(rds, cache_execute, "arn", "sec", "c1", "db", "2026-06-26T00:00:00Z")

    metric_types = {p["metric_type"] for t, p in calls if t == "metric"}
    assert "innodb_history_list_length" in metric_types
    assert "innodb_buffer_pool_hit_rate" in metric_types
    assert res["metrics_inserted"] == 5  # HLL, hit, checkpoint_age, pending, row_ops
    # HLL 2.5M > 1M warn threshold (but < 10M crit) → one warning finding.
    findings = [p for t, p in calls if t == "finding"]
    assert len(findings) == 1
    assert findings[0]["check_type"] == "innodb_history_list_high"
    assert findings[0]["severity"] == "warning"


def test_collect_mysql_innodb_status_crit_above_10m():
    rds = MagicMock()
    rds.execute_statement.return_value = {
        "records": [[{"stringValue": "InnoDB"}, {"stringValue": ""},
                     {"stringValue": "History list length 20000000\n"}]]
    }
    calls, cache_execute = _capture()
    my.collect_mysql_innodb_status(rds, cache_execute, "a", "s", "c1", "db", "2026-06-26T00:00:00Z")
    findings = [p for t, p in calls if t == "finding"]
    assert findings and findings[0]["severity"] == "critical"


# ── PG engine internals ──────────────────────────────────────────────────────

def _pg_rds(cache_hit=85.0, rollback=7.0, temp=123456.0, forced=40.0):
    rds = MagicMock()

    def _exec(**kwargs):
        sql = kwargs["sql"]
        if "pg_stat_database" in sql:
            return {"records": [[{"doubleValue": cache_hit}, {"doubleValue": rollback},
                                 {"doubleValue": temp}]]}
        if "pg_stat_bgwriter" in sql:
            return {"records": [[{"doubleValue": forced}]]}
        return {"records": []}

    rds.execute_statement.side_effect = _exec
    return rds


def test_collect_pg_engine_internals_metrics_and_findings():
    rds = _pg_rds(cache_hit=85.0, rollback=7.0, forced=40.0)
    calls, cache_execute = _capture()
    res = pg.collect_pg_engine_internals(rds, cache_execute, "arn", "sec", "c1", "db", "2026-06-26T00:00:00Z")

    metric_types = {p["metric_type"] for t, p in calls if t == "metric"}
    assert metric_types == {
        "pg_cache_hit_ratio", "pg_rollback_ratio", "pg_temp_bytes", "pg_checkpoint_forced_ratio",
    }
    checks = {p["check_type"] for t, p in calls if t == "finding"}
    # cache_hit 85<90, rollback 7>5, forced 40>30 → all three findings.
    assert checks == {"pg_cache_hit_low", "pg_rollback_high", "pg_forced_checkpoints_high"}
    assert res["findings"] == 3


def test_collect_pg_engine_internals_healthy_emits_no_findings():
    rds = _pg_rds(cache_hit=99.5, rollback=0.2, forced=5.0)
    calls, cache_execute = _capture()
    res = pg.collect_pg_engine_internals(rds, cache_execute, "a", "s", "c1", "db", "2026-06-26T00:00:00Z")
    assert res["findings"] == 0
    assert res["metrics_inserted"] == 4


def test_collect_pg_engine_internals_idle_cache_hit_zero_no_finding():
    # An idle cluster with no reads → cache_hit NULL→0.0; must NOT flag (0<90 but
    # the `0 < cache_hit` guard skips it).
    rds = _pg_rds(cache_hit=0.0, rollback=0.0, forced=0.0)
    calls, cache_execute = _capture()
    pg.collect_pg_engine_internals(rds, cache_execute, "a", "s", "c1", "db", "2026-06-26T00:00:00Z")
    assert not [p for t, p in calls if t == "finding"]


def test_collect_pg_engine_internals_bgwriter_failure_keeps_db_stats():
    # PG17 renamed pg_stat_bgwriter → the bgwriter query raises; db-stats metrics
    # + findings must still be collected (partial success, never raises).
    rds = MagicMock()

    def _exec(**kwargs):
        if "pg_stat_bgwriter" in kwargs["sql"]:
            raise Exception("relation pg_stat_bgwriter does not exist")
        return {"records": [[{"doubleValue": 85.0}, {"doubleValue": 1.0}, {"doubleValue": 0.0}]]}

    rds.execute_statement.side_effect = _exec
    calls, cache_execute = _capture()
    res = pg.collect_pg_engine_internals(rds, cache_execute, "a", "s", "c1", "db", "2026-06-26T00:00:00Z")
    metric_types = {p["metric_type"] for t, p in calls if t == "metric"}
    assert "pg_cache_hit_ratio" in metric_types
    assert "pg_checkpoint_forced_ratio" not in metric_types
    assert any("pg_stat_bgwriter" in e for e in res["errors"])
