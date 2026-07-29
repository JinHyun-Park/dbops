"""sys.dm_os_performance_counters -> metric_snapshots (E-3).

The rows below are the LIVE result set from dbops-demo-mssql, read through the
deployed operations MCP Lambda. Two probes minutes apart returned
`Buffer cache hit ratio` = 1980 with base 1980, then 104 with base 104: the raw
`cntr_value` moves with the counter window and is NOT a percentage, while the
derived ratio is 100.0% both times. That is the whole reason this collector
exists, so the first test pins it with both measured pairs.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "rds_direct_collector"


def _load(name, rel):
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pc = _load("mssql_perf_counters", "mssql_perf_counters.py")

BUF = "SQLServer:Buffer Manager"
MEM = "SQLServer:Memory Manager"
GEN = "SQLServer:General Statistics"

# MEASURED live (second probe): obj, counter, cntr_value, cntr_type
_LIVE = [
    (BUF, "Buffer cache hit ratio", 104, 537003264),
    (BUF, "Buffer cache hit ratio base", 104, 1073939712),
    (BUF, "Page life expectancy", 10449, 65792),
    (GEN, "Processes blocked", 0, 65792),
    (MEM, "Memory Grants Pending", 0, 65792),
    (MEM, "Target Server Memory (KB)", 496752, 65792),
    (MEM, "Total Server Memory (KB)", 184488, 65792),
]


def _f(v):
    return {"longValue": v} if isinstance(v, int) else {"stringValue": v}


class _Fake:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.writes = []

    def execute_statement(self, **kw):
        self.sql = kw["sql"]
        return {"records": [[_f(a), _f(b), _f(c), _f(d)] for a, b, c, d in self.rows]}

    def cache_execute(self, sql, params):
        self.writes.append(params)


def _run(rows):
    fake = _Fake(rows)
    result = pc.collect_mssql_perf_counters(
        fake, fake.cache_execute, "", "", "dbops-demo-mssql", "master")
    return fake, result


def test_buffer_cache_hit_is_derived_from_its_base_not_published_raw():
    """cntr_type 537003264 is a ratio NUMERATOR. Publishing cntr_value would
    report a healthy 100% cache as "104" (and as "1980" ten minutes earlier)."""
    _, result = _run(_LIVE)
    assert result["metrics"]["mssql_buffer_cache_hit_ratio"] == 100.0
    # The first probe's raw numbers derive to the same ratio, the raw values do not.
    _, other = _run([
        (BUF, "Buffer cache hit ratio", 1980, 537003264),
        (BUF, "Buffer cache hit ratio base", 1980, 1073939712),
    ])
    assert other["metrics"]["mssql_buffer_cache_hit_ratio"] == 100.0


def test_partial_ratio_derives_the_real_percentage():
    _, result = _run([
        (BUF, "Buffer cache hit ratio", 940, 537003264),
        (BUF, "Buffer cache hit ratio base", 1000, 1073939712),
    ])
    assert result["metrics"]["mssql_buffer_cache_hit_ratio"] == 94.0


def test_zero_base_is_skipped_with_a_reason_not_reported_as_a_percentage():
    """No page lookups yet means the ratio is UNDEFINED. 0% and 100% are both
    inventions; the metric must be absent and the reason recorded."""
    fake, result = _run([
        (BUF, "Buffer cache hit ratio", 0, 537003264),
        (BUF, "Buffer cache hit ratio base", 0, 1073939712),
    ])
    assert "mssql_buffer_cache_hit_ratio" not in result["metrics"]
    assert "base is 0" in result["skipped"]["mssql_buffer_cache_hit_ratio"]
    assert fake.writes == []


def test_missing_base_row_is_skipped_not_guessed():
    _, result = _run([(BUF, "Buffer cache hit ratio", 104, 537003264)])
    assert "mssql_buffer_cache_hit_ratio" not in result["metrics"]
    assert result["skipped"]["mssql_buffer_cache_hit_ratio"]


def test_raw_gauges_published_as_read():
    _, result = _run(_LIVE)
    m = result["metrics"]
    assert m["mssql_page_life_expectancy_sec"] == 10449.0
    assert m["mssql_memory_grants_pending"] == 0.0
    assert m["mssql_processes_blocked"] == 0.0


def test_server_memory_ratio_is_total_over_target():
    _, result = _run(_LIVE)
    # 184488 / 496752 = 37.14%, MEASURED operands.
    assert result["metrics"]["mssql_server_memory_used_pct"] == 37.14


def test_unexpected_cntr_type_refuses_rather_than_publishing_a_wrong_meaning():
    """If the engine ever reports one of these as cumulative, the number means
    something else and must not be published under the same metric_type."""
    _, result = _run([(BUF, "Page life expectancy", 10449, 272696576)])
    assert "mssql_page_life_expectancy_sec" not in result["metrics"]
    assert "unexpected cntr_type 272696576" in result["skipped"]["mssql_page_life_expectancy_sec"]


def test_no_cumulative_counter_is_ever_collected():
    """cntr_type 272696576 counters are cumulative since server start. A
    monotonic series in metric_snapshots would train a baseline it always exceeds
    (pg_baseline_trainer and proactive_monitor iterate EVERY metric_type with no
    allowlist), emitting a false anomaly every cycle forever."""
    fake, result = _run(_LIVE)
    # The statement itself must not ask for any of them.
    for cumulative in ("Batch Requests/sec", "Number of Deadlocks/sec",
                       "Lock Waits/sec", "Transactions/sec"):
        assert cumulative not in fake.sql
    assert len(result["metrics"]) == 5


def test_all_rows_are_cluster_level_and_instance_pinned():
    """dimensions must be '{}' so cluster-level readers see these, and the
    statement must pin instance_name='' or a multi-instance counter (Lock Waits
    has 15 rows, Page life expectancy also appears under Buffer Node '000')
    would be counted more than once."""
    fake, _ = _run(_LIVE)
    assert "RTRIM(instance_name) = ''" in fake.sql
    assert fake.writes and all(w["dimensions"] == "{}" for w in fake.writes)
    assert len({w["metric_type"] for w in fake.writes}) == len(fake.writes)


def test_empty_result_set_writes_nothing():
    fake, result = _run([])
    assert fake.writes == []
    assert result["metrics"] == {}
    assert result["counters_read"] == 0


def test_marker_and_dimensions_shape():
    fake, _ = _run(_LIVE)
    assert "/* source=dbops-etl */" in fake.sql
    assert "ON CONFLICT DO NOTHING" in pc.INSERT_METRIC


def test_collector_does_not_raise_on_a_bare_mock_adapter():
    """The handler wraps each collector in try/except, but a collector that
    raises on an empty/odd response still loses its whole signal. A MagicMock
    response yields no records and must be treated as no data."""
    adapter = MagicMock()
    adapter.execute_statement.return_value = {}
    result = pc.collect_mssql_perf_counters(
        adapter, lambda sql, p: None, "", "", "c", "master")
    assert result["metrics"] == {}
