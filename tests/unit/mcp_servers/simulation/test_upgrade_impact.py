from unittest.mock import MagicMock

from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.upgrade_impact import estimate_upgrade_impact_impl


def test_estimate_upgrade_impact_returns_methods():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["storage_size_gb", "engine_version"],
        rows=[{"storage_size_gb": "200", "engine_version": "14.6"}],
        row_count=1,
    )

    result = estimate_upgrade_impact_impl(mock_cache, cluster_id="prod-pg-1", target_version="15.4")

    assert result["cluster_id"] == "prod-pg-1"
    assert result["target_version"] == "15.4"
    assert result["storage_gb"] == 200.0
    assert len(result["methods"]) == 3
    method_names = [m["method"] for m in result["methods"]]
    assert "in_place" in method_names
    assert "blue_green" in method_names
    assert "clone" in method_names
    assert result["recommendation"] == "blue_green"


def test_estimate_upgrade_impact_empty_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)

    result = estimate_upgrade_impact_impl(mock_cache, cluster_id="unknown", target_version="15.4")

    assert result["storage_gb"] == 50.0  # default
    assert len(result["methods"]) == 3
