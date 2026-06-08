"""Unit tests for the simulation REST handler's parameter-change path.

Pin the REST mirror to the shared LIVE-metadata model: it reads the cluster's
real parameter group (ApplyType/IsModifiable/AllowedValues) instead of a static
catalog, and degrades to the coarse heuristic only when the describe fails.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SIM_DIR = ROOT / "api" / "simulation"
HANDLER_PATH = SIM_DIR / "handler.py"
sys.path.insert(0, str(SIM_DIR))


def _load():
    spec = importlib.util.spec_from_file_location("simulation_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["simulation_handler"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def mod():
    return _load()


def _rds_with_param(pg_name, param_row):
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterParameterGroup": pg_name}]
    }
    rds.describe_db_cluster_parameters.return_value = {"Parameters": [param_row], "Marker": None}
    return rds


def test_parameter_live_read_static_requires_restart(mod, monkeypatch):
    rds = _rds_with_param(
        "custom-pg16",
        {
            "ParameterName": "shared_buffers",
            "ApplyType": "static",
            "ParameterValue": "8192",
            "IsModifiable": True,
            "AllowedValues": "16-1073741823",
            "DataType": "integer",
        },
    )
    fake_boto = MagicMock()
    fake_boto.client.return_value = rds
    monkeypatch.setattr(mod, "boto3", fake_boto)

    out = mod._simulate_parameter_change("c", "shared_buffers", "16384")
    assert out["data_source"].startswith("live")
    assert out["requires_restart"] is True
    assert out["is_dynamic"] is False
    assert out["current_value"] == "8192"
    assert out["parameter_group"] == "custom-pg16"
    assert out["known"] is True


def test_parameter_live_read_dynamic_immediate(mod, monkeypatch):
    rds = _rds_with_param(
        "custom-pg16",
        {
            "ParameterName": "work_mem",
            "ApplyType": "dynamic",
            "ParameterValue": "4096",
            "IsModifiable": True,
        },
    )
    fake_boto = MagicMock()
    fake_boto.client.return_value = rds
    monkeypatch.setattr(mod, "boto3", fake_boto)

    out = mod._simulate_parameter_change("c", "work_mem", "8192")
    assert out["is_dynamic"] is True
    assert out["requires_restart"] is False


def test_parameter_default_group_falls_back(mod, monkeypatch):
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterParameterGroup": "default.aurora-postgresql16"}]
    }
    fake_boto = MagicMock()
    fake_boto.client.return_value = rds
    monkeypatch.setattr(mod, "boto3", fake_boto)

    out = mod._simulate_parameter_change("c", "work_mem", "8192")
    assert "static fallback" in out["data_source"]
    assert out["known"] is True  # work_mem is in the fallback catalog


def test_parameter_describe_failure_falls_back(mod, monkeypatch):
    rds = MagicMock()
    rds.describe_db_clusters.side_effect = RuntimeError("not reachable")
    fake_boto = MagicMock()
    fake_boto.client.return_value = rds
    monkeypatch.setattr(mod, "boto3", fake_boto)

    out = mod._simulate_parameter_change("c", "max_connections", "2000")
    assert "static fallback" in out["data_source"]
    assert out["requires_restart"] is True  # max_connections is static in fallback
