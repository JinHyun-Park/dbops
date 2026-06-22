import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "data-pipeline" / "etl_collector" / "collectors" / "cw_collector.py"
_spec = importlib.util.spec_from_file_location("cw_collector", PATH)
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)


def _dp(ts="2026-06-22T00:00:00+00:00", val=42.0):
    import datetime
    return {"Timestamp": datetime.datetime.fromisoformat(ts), "Average": val}


def test_per_instance_uses_instance_dimension_and_tags_rows():
    client = MagicMock()
    client.get_metric_statistics.return_value = {"Datapoints": [_dp()]}
    writes = []
    def cache_execute(sql, params):
        writes.append((sql, params))
    instances = [{"id": "w1", "role": "writer"}, {"id": "r1", "role": "reader"}]

    out = cw.collect_cw_instance_metrics(client, cache_execute, "c1", instances)

    # CloudWatch queried with DBInstanceIdentifier per instance
    dims = [
        call.kwargs["Dimensions"][0]
        for call in client.get_metric_statistics.call_args_list
    ]
    assert {"Name": "DBInstanceIdentifier", "Value": "w1"} in dims
    assert {"Name": "DBInstanceIdentifier", "Value": "r1"} in dims
    # rows tagged with instance + role in dimensions
    tagged = [json.loads(p["dimensions"]) for _, p in writes]
    assert {"instance": "w1", "role": "writer"} in tagged
    assert {"instance": "r1", "role": "reader"} in tagged
    assert out["metrics_inserted"] == len(writes) > 0


def test_no_instances_is_noop():
    client = MagicMock()
    out = cw.collect_cw_instance_metrics(client, lambda *a: None, "c1", [])
    assert out["metrics_inserted"] == 0
    client.get_metric_statistics.assert_not_called()
