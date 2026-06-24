"""ElastiCache CloudWatch collector → metric_snapshots."""
import importlib.util
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[3] / "data-pipeline/etl_collector/collectors/elasticache_cw_collector.py"
_spec = importlib.util.spec_from_file_location("ec_collector", _C)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _cw_with(value=42.0):
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {
        "Datapoints": [{"Timestamp": datetime(2026, 6, 24, 0, 0, 0), "Average": value, "Sum": value}]
    }
    return cw


def test_redis_metrics_inserted_with_types():
    cw = _cw_with()
    ec = MagicMock()
    rows = []
    def cache_execute(sql, params=None):
        if params and "metric_type" in params:
            rows.append(params["metric_type"])
    res = mod.collect_elasticache_metrics(cw, ec, cache_execute, "my-redis", "my-redis", "redis", "ap-northeast-2", "111122223333")
    assert res["metrics_inserted"] > 0
    # representative Redis metric types present
    assert "memory_usage_pct" in rows
    assert "evictions" in rows
    assert "curr_connections" in rows


def test_memcached_branch_subset():
    cw = _cw_with()
    ec = MagicMock()
    rows = []
    def cache_execute(sql, params=None):
        if params and "metric_type" in params:
            rows.append(params["metric_type"])
    mod.collect_elasticache_metrics(cw, ec, cache_execute, "mc", "mc", "memcached", "ap-northeast-2", "111122223333")
    # memcached has no replication lag / DatabaseMemoryUsagePercentage
    assert "replication_lag" not in rows
    assert "get_hits" in rows or "curr_items" in rows


def test_empty_series_tolerated():
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": []}
    ec = MagicMock()
    res = mod.collect_elasticache_metrics(cw, ec, lambda *a, **k: None, "r", "r", "redis", "ap-northeast-2", "1")
    assert res["metrics_inserted"] == 0  # no rows, no raise
