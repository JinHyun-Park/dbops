"""Unit tests for the rds_instance CloudWatch + meta collector.

Standalone RDS instances (non-Aurora MySQL / SQL Server) never expose
DBClusterIdentifier — every CW call must be instance-dimensioned, and rows land
with dimensions='{}' (the instance IS the monitored resource).
"""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load():
    spec = importlib.util.spec_from_file_location(
        "rds_instance_cw_collector",
        _ROOT / "collectors/rds_instance_cw_collector.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mod = _load()


def _mk_clients():
    rds = MagicMock()
    rds.describe_db_instances.return_value = {"DBInstances": [{
        "DBInstanceIdentifier": "dbops-demo-mysql", "Engine": "mysql",
        "EngineVersion": "8.0.42", "DBInstanceClass": "db.t4g.micro",
        "DBInstanceStatus": "available", "MultiAZ": False,
        "StorageType": "gp3", "AllocatedStorage": 20, "Iops": 3000,
        "LicenseModel": "general-public-license", "PubliclyAccessible": False,
        "PerformanceInsightsEnabled": True, "DbiResourceId": "db-ABC",
        "Endpoint": {"Address": "x.rds.amazonaws.com", "Port": 3306},
    }]}
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": [
        {"Timestamp": datetime(2026, 7, 22, 5, 0), "Average": 12.5}]}
    return cw, rds


def test_collect_uses_instance_dimension_and_writes_meta():
    cw, rds = _mk_clients()
    calls = []
    def cache_execute(sql, params=None):
        calls.append((sql, params))
    r = mod.collect_rds_instance_metrics(cw, rds, cache_execute,
                                         "dbops-demo-mysql", "ap-northeast-2", "123")
    assert r["resource_id"] == "db-ABC" and r["pi_enabled"] is True
    assert r["metrics_inserted"] > 0
    # Every CW call must be instance-dimensioned — DBClusterIdentifier does not
    # exist for standalone instances.
    for c in cw.get_metric_statistics.call_args_list:
        assert c.kwargs["Namespace"] == "AWS/RDS"
        assert c.kwargs["Dimensions"] == [
            {"Name": "DBInstanceIdentifier", "Value": "dbops-demo-mysql"}]
    meta_calls = [p for (s, p) in calls if "cluster_meta" in s]
    assert meta_calls, "cluster_meta upsert missing"
    details = json.loads(meta_calls[0]["details"])
    assert details["instance_class"] == "db.t4g.micro"
    assert details["pi_enabled"] is True
    # Provisioned IOPS captured for the right-sizing cost sim (gp3 baseline here).
    assert details["iops"] == 3000
    metric_calls = [p for (s, p) in calls if "metric_snapshots" in s]
    assert {p["metric_type"] for p in metric_calls} >= {"cpu"}


def test_describe_failure_is_nonfatal():
    cw, rds = _mk_clients()
    rds.describe_db_instances.side_effect = Exception("boom")
    r = mod.collect_rds_instance_metrics(cw, rds, lambda *a, **k: None,
                                         "x", "ap-northeast-2", "123")
    assert r["resource_id"] is None
    assert any("describe_db_instances" in e for e in r["errors"])
