from unittest.mock import MagicMock, patch
from mcp_servers.operations.tools.manage_maintenance import manage_maintenance_impl


@patch("mcp_servers.operations.tools.manage_maintenance.boto3")
def test_manage_maintenance_describe(mock_boto3):
    mock_rds = MagicMock()
    mock_boto3.client.return_value = mock_rds
    mock_rds.describe_db_clusters.return_value = {
        "DBClusters": [{
            "PreferredMaintenanceWindow": "sun:03:00-sun:04:00",
            "PendingModifiedValues": {},
        }]
    }
    mock_cache = MagicMock()
    result = manage_maintenance_impl(mock_cache, cluster_id="prod-pg-1", action="describe")
    assert result["maintenance_window"] == "sun:03:00-sun:04:00"
    assert result["cluster_id"] == "prod-pg-1"


def test_manage_maintenance_modify_requires_approval():
    mock_cache = MagicMock()
    result = manage_maintenance_impl(mock_cache, cluster_id="prod-pg-1", action="modify", window="mon:03:00-mon:04:00")
    assert result["status"] == "approval_required"


@patch("mcp_servers.operations.tools.manage_maintenance.boto3")
def test_manage_maintenance_modify_with_approval(mock_boto3):
    mock_rds = MagicMock()
    mock_boto3.client.return_value = mock_rds
    mock_cache = MagicMock()
    result = manage_maintenance_impl(mock_cache, cluster_id="prod-pg-1", action="modify", window="mon:03:00-mon:04:00", approved=True)
    assert result["status"] == "modified"
    assert result["new_window"] == "mon:03:00-mon:04:00"


def test_manage_maintenance_unknown_action():
    mock_cache = MagicMock()
    result = manage_maintenance_impl(mock_cache, cluster_id="prod-pg-1", action="unknown")
    assert "error" in result
