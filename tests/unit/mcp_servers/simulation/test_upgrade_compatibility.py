from unittest.mock import MagicMock, patch
from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.upgrade_compatibility import check_upgrade_compatibility_impl


def test_check_upgrade_compatibility_compatible():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["engine", "engine_version"],
        rows=[{"engine": "aurora-postgresql", "engine_version": "14.6"}],
        row_count=1,
    )

    mock_rds = MagicMock()
    mock_rds.describe_db_engine_versions.side_effect = [
        # First call: target version info
        {
            "DBEngineVersions": [
                {
                    "EngineVersion": "15.4",
                    "DBEngineVersionDescription": "Aurora PostgreSQL 15.4",
                }
            ]
        },
        # Second call: valid upgrade targets from current version
        {
            "DBEngineVersions": [
                {
                    "ValidUpgradeTarget": [
                        {"EngineVersion": "14.9"},
                        {"EngineVersion": "15.4"},
                    ]
                }
            ]
        },
    ]

    with patch("mcp_servers.simulation.tools.upgrade_compatibility.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_rds
        result = check_upgrade_compatibility_impl(mock_cache, cluster_id="prod-pg-1", target_version="15.4")

    assert result["cluster_id"] == "prod-pg-1"
    assert result["current_version"] == "14.6"
    assert result["target_version"] == "15.4"
    assert result["is_compatible"] is True
    assert "15.4" in result["valid_upgrade_targets"]
    assert result["target_info"]["version"] == "15.4"


def test_check_upgrade_compatibility_incompatible():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["engine", "engine_version"],
        rows=[{"engine": "aurora-postgresql", "engine_version": "13.9"}],
        row_count=1,
    )

    mock_rds = MagicMock()
    mock_rds.describe_db_engine_versions.side_effect = [
        {"DBEngineVersions": [{"EngineVersion": "16.0", "DBEngineVersionDescription": "Aurora PostgreSQL 16.0"}]},
        {"DBEngineVersions": [{"ValidUpgradeTarget": [{"EngineVersion": "14.6"}]}]},
    ]

    with patch("mcp_servers.simulation.tools.upgrade_compatibility.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_rds
        result = check_upgrade_compatibility_impl(mock_cache, cluster_id="prod-pg-1", target_version="16.0")

    assert result["is_compatible"] is False
