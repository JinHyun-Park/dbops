"""Tests for request_approval — verify all action_types including EC-4 are accepted."""
import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

_BASE = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/operations/tools"


def _load(name):
    p = _BASE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


request_approval = _load("request_approval")


def test_request_approval_accepts_execute_sql():
    """request_approval accepts execute_sql action_type."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cluster", action_type="execute_sql",
                action_details={"sql": "SELECT 1"}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "execute_sql"


def test_request_approval_accepts_modify_elasticache_node_type():
    """EC-4: request_approval accepts modify_elasticache_node_type."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="modify_elasticache_node_type",
                action_details={"node_type": "cache.r7g.large"}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "modify_elasticache_node_type"


def test_request_approval_accepts_create_elasticache_snapshot():
    """EC-4: request_approval accepts create_elasticache_snapshot."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="create_elasticache_snapshot",
                action_details={"snapshot_name": "snap1"}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "create_elasticache_snapshot"


def test_request_approval_accepts_reboot_elasticache():
    """EC-4: request_approval accepts reboot_elasticache."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="reboot_elasticache",
                action_details={}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "reboot_elasticache"


def test_request_approval_accepts_test_elasticache_failover():
    """EC-4: request_approval accepts test_elasticache_failover."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="test_elasticache_failover",
                action_details={}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "test_elasticache_failover"


def test_request_approval_rejects_unknown_action_type():
    """request_approval rejects unknown action_types as expected."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cluster", action_type="unknown_action_xyz",
                action_details={}
            )
            assert result["status"] == "error"
            assert "unknown action_type" in result["message"]
