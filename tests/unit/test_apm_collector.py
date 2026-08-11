# tests/unit/test_apm_collector.py
import importlib.util
from datetime import datetime
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "apm_collector",
    Path(__file__).resolve().parents[2] / "data-pipeline/etl_collector/collectors/apm_collector.py",
)
apm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(apm)


class FakeCW:
    def get_metric_statistics(self, **kw):
        return {"Datapoints": [{"Timestamp": datetime(2026, 8, 11, 0, 0), "Average": 42.0}]}


class FakeLogs:
    def start_query(self, **kw):
        return {"queryId": "q1"}

    def get_query_results(self, **kw):
        return {"status": "Complete", "results": [
            [{"field": "level", "value": "ERROR"}, {"field": "cnt", "value": "5"}],
            [{"field": "level", "value": "WARN"}, {"field": "cnt", "value": "3"}],
        ]}


def test_collect_apm_writes_metrics_and_log_counts():
    rows = []
    def cache_execute(sql, params):
        rows.append((sql, params))
    target = {"target_id": "svc-a", "instance_id": "i-1", "region": "ap-northeast-2",
              "service_name": "orders", "log_groups": ["/app/orders"], "team": ""}

    result = apm.collect_apm(FakeCW(), FakeLogs(), cache_execute, target)

    assert result["target_id"] == "svc-a"
    assert result["metrics_inserted"] > 0
    assert result["log_buckets_inserted"] == 2  # ERROR + WARN buckets
    joined = " ".join(sql for sql, _ in rows)
    assert "apm_metric_snapshots" in joined
    assert "apm_log_level_counts" in joined
    assert "apm_target_meta" in joined


def test_collect_apm_records_errors_but_does_not_raise():
    class BoomCW:
        def get_metric_statistics(self, **kw):
            raise RuntimeError("throttled")
    target = {"target_id": "svc-b", "instance_id": "i-2", "region": "ap-northeast-2",
              "service_name": "x", "log_groups": [], "team": ""}
    result = apm.collect_apm(BoomCW(), FakeLogs(), lambda s, p: None, target)
    assert result["errors"]  # non-empty
    assert result["target_id"] == "svc-b"


class CapturingLogs(FakeLogs):
    def __init__(self):
        self.start_kwargs = None

    def start_query(self, **kw):
        self.start_kwargs = kw
        return {"queryId": "q1"}


def test_start_query_uses_epoch_seconds_not_millis():
    logs = CapturingLogs()
    target = {"target_id": "svc-c", "instance_id": "i-3", "region": "ap-northeast-2",
              "service_name": "orders", "log_groups": ["/app/orders"], "team": ""}
    apm.collect_apm(FakeCW(), logs, lambda s, p: None, target)
    # Epoch SECONDS are ~1.7e9 today; MILLISECONDS would be ~1.7e12 (year ~53000).
    assert logs.start_kwargs["startTime"] < 10_000_000_000
    assert logs.start_kwargs["endTime"] < 10_000_000_000
    assert logs.start_kwargs["startTime"] < logs.start_kwargs["endTime"]


def test_query_timeout_records_error():
    class NeverCompleteLogs:
        def start_query(self, **kw):
            return {"queryId": "q1"}

        def get_query_results(self, **kw):
            return {"status": "Running", "results": []}
    target = {"target_id": "svc-d", "instance_id": "i-4", "region": "ap-northeast-2",
              "service_name": "orders", "log_groups": ["/app/orders"], "team": ""}
    # Avoid 25s of real sleeping: neutralize the poll delay.
    orig_sleep = apm.time.sleep
    apm.time.sleep = lambda *_a, **_k: None
    try:
        result = apm.collect_apm(FakeCW(), NeverCompleteLogs(), lambda s, p: None, target)
    finally:
        apm.time.sleep = orig_sleep
    assert any("timed out" in e for e in result["errors"])
    assert result["log_buckets_inserted"] == 0


def test_cache_execute_failure_does_not_raise():
    def boom_execute(sql, params):
        raise RuntimeError("db down")
    target = {"target_id": "svc-e", "instance_id": "i-5", "region": "ap-northeast-2",
              "service_name": "orders", "log_groups": ["/app/orders"], "team": ""}
    result = apm.collect_apm(FakeCW(), FakeLogs(), boom_execute, target)
    assert result["errors"]  # errors captured, no exception propagated
    assert result["target_id"] == "svc-e"
