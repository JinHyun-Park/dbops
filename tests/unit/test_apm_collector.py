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
