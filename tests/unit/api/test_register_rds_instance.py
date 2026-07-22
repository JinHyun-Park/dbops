"""Tests for standalone RDS instance registration + dispatch (R-1 Task 2).

Covers _register_rds_instance:
  - happy path (mysql) → 201, engine/family/resource_type/port stored
  - cluster member → 400, no registry row (hard-fail, unlike Aurora's 207)
  - describe error → 400, no row, raw exception text NOT leaked
  - _handle_register dispatches engine='sqlserver' to _register_rds_instance
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_CLUSTERS_DIR = Path(__file__).resolve().parents[3] / "api" / "clusters"
# Push clusters/ dir so `import seeder` and `import engine_family` both resolve.
sys.path.insert(0, str(_CLUSTERS_DIR))

_PATH = _CLUSTERS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("clusters_handler_rds_inst", _PATH)
_handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_handler)


@pytest.fixture(autouse=True)
def _clusters_table_env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


@pytest.fixture
def handler_module():
    return _handler


@pytest.fixture
def mock_table():
    t = MagicMock()
    t.put_item = MagicMock()
    return t


def _mk_instance(engine="mysql", cluster_member=None):
    inst = {
        "DBInstanceIdentifier": "dbops-demo-mysql",
        "Engine": engine, "EngineVersion": "8.0.42",
        "DBInstanceStatus": "available",
        "Endpoint": {"Address": "demo.x.ap-northeast-2.rds.amazonaws.com", "Port": 3306},
    }
    if cluster_member:
        inst["DBClusterIdentifier"] = cluster_member
    return inst


def test_register_rds_instance_happy_path(handler_module, mock_table):
    h = handler_module
    with patch.object(h, "_rds_client_for") as rds:
        rds.return_value.describe_db_instances.return_value = {"DBInstances": [_mk_instance()]}
        resp = h._register_rds_instance(mock_table, {
            "cluster_id": "dbops-demo-mysql", "account_id": "123", "region": "ap-northeast-2"})
    assert resp["statusCode"] == 201
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["engine_family"] == "rds_instance"
    assert item["engine"] == "mysql"
    assert item["resource_type"] == "rds-mysql"
    assert item["port"] == 3306


def test_register_rejects_cluster_member(handler_module, mock_table):
    h = handler_module
    with patch.object(h, "_rds_client_for") as rds:
        rds.return_value.describe_db_instances.return_value = {
            "DBInstances": [_mk_instance(engine="aurora-mysql", cluster_member="my-aurora")]}
        resp = h._register_rds_instance(mock_table, {
            "cluster_id": "my-aurora-instance-1", "account_id": "123", "region": "ap-northeast-2"})
    assert resp["statusCode"] == 400
    mock_table.put_item.assert_not_called()


def test_register_hard_fails_on_describe_error(handler_module, mock_table):
    h = handler_module
    with patch.object(h, "_rds_client_for") as rds:
        rds.return_value.describe_db_instances.side_effect = Exception("AccessDenied secret-sauce")
        resp = h._register_rds_instance(mock_table, {
            "cluster_id": "nope", "account_id": "123", "region": "ap-northeast-2"})
    assert resp["statusCode"] == 400
    mock_table.put_item.assert_not_called()
    # Static reason — the raw exception text must NOT leak into the response.
    assert "secret-sauce" not in resp["body"]


def test_handle_register_dispatches_rds_instance(handler_module, mock_table):
    h = handler_module
    with patch.object(h, "_register_rds_instance") as reg:
        reg.return_value = {"statusCode": 201, "body": "{}"}
        h._handle_register(mock_table, {"engine": "sqlserver", "cluster_id": "x",
                                        "account_id": "1", "region": "ap-northeast-2"})
        reg.assert_called_once()
