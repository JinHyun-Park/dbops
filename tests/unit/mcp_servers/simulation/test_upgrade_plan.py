from unittest.mock import MagicMock
from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.upgrade_plan import generate_upgrade_plan_impl


def test_generate_upgrade_plan_blue_green():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["cluster_id", "engine_version"],
        rows=[{"cluster_id": "prod-pg-1", "engine_version": "14.6"}],
        row_count=1,
    )

    result = generate_upgrade_plan_impl(mock_cache, cluster_id="prod-pg-1", target_version="15.4", method="blue_green")

    assert result["cluster_id"] == "prod-pg-1"
    assert result["method"] == "blue_green"
    assert len(result["steps"]) == 9
    assert "rollback_plan" in result
    assert "Blue" in result["rollback_plan"]


def test_generate_upgrade_plan_in_place():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)

    result = generate_upgrade_plan_impl(mock_cache, cluster_id="prod-pg-1", target_version="15.4", method="in_place")

    assert result["method"] == "in_place"
    assert len(result["steps"]) == 7
    assert "스냅샷" in result["rollback_plan"]
