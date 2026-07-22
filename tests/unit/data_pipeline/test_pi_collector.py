"""Unit tests for the PI collector's injectable metric list.

rds_instance engines (MySQL/SQL Server RDS instances) can't use the full
Aurora-shaped PI_METRIC_QUERIES list — PI's GetResourceMetrics rejects the
whole batched call if any single metric is unknown for the engine. The
`metrics` param lets callers swap in an engine-safe list; default (None)
preserves the existing Aurora behavior.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load():
    spec = importlib.util.spec_from_file_location(
        "pi_collector", _ROOT / "collectors/pi_collector.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mod = _load()


def test_default_metrics_uses_full_aurora_list():
    pi_client = MagicMock()
    pi_client.get_resource_metrics.return_value = {"MetricList": []}

    mod.collect_pi_metrics(pi_client, lambda sql, params: None, "db-ABC", "cl-1")

    queries = pi_client.get_resource_metrics.call_args.kwargs["MetricQueries"]
    assert len(queries) == len(mod.PI_METRIC_QUERIES)
    assert {q["Metric"] for q in queries} == {q["Metric"] for q in mod.PI_METRIC_QUERIES}


def test_custom_metrics_queries_only_the_given_list():
    pi_client = MagicMock()
    pi_client.get_resource_metrics.return_value = {"MetricList": []}
    reduced = [{"Metric": "db.load.avg", "GroupBy": {"Group": "db.wait_event"}, "metric_type": "aas"}]

    mod.collect_pi_metrics(
        pi_client, lambda sql, params: None, "db-ABC", "cl-1", metrics=reduced)

    queries = pi_client.get_resource_metrics.call_args.kwargs["MetricQueries"]
    assert queries == [{"Metric": "db.load.avg", "GroupBy": {"Group": "db.wait_event"}}]


def test_rds_instance_constant_is_load_avg_only():
    assert mod.PI_METRICS_RDS_INSTANCE == [
        {"Metric": "db.load.avg", "GroupBy": {"Group": "db.wait_event"}, "metric_type": "aas"},
    ]
