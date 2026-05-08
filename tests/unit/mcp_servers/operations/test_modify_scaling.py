from unittest.mock import MagicMock, patch
from mcp_servers.operations.tools.modify_scaling import modify_scaling_impl


def test_modify_scaling_requires_approval():
    mock_cache = MagicMock()
    result = modify_scaling_impl(mock_cache, cluster_id="prod-pg-1", min_capacity=0.5, max_capacity=4.0)
    assert result["status"] == "approval_required"
    assert result["min_capacity"] == 0.5
    assert result["max_capacity"] == 4.0


@patch("mcp_servers.operations.tools.modify_scaling.boto3")
def test_modify_scaling_with_approval(mock_boto3):
    mock_rds = MagicMock()
    mock_boto3.client.return_value = mock_rds
    mock_cache = MagicMock()
    result = modify_scaling_impl(mock_cache, cluster_id="prod-pg-1", min_capacity=0.5, max_capacity=4.0, approved=True)
    assert result["status"] == "modified"
    assert result["scaling"] == {"MinCapacity": 0.5, "MaxCapacity": 4.0}
    mock_rds.modify_db_cluster.assert_called_once()
