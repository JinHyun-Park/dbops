from unittest.mock import MagicMock
from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.scaling_simulation import simulate_scaling_impl


def test_simulate_scaling_returns_cost_impact():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["cluster_id"],
        rows=[{"cluster_id": "prod-pg-1"}],
        row_count=1,
    )

    result = simulate_scaling_impl(mock_cache, cluster_id="prod-pg-1", new_min_acu=1.0, new_max_acu=8.0)

    assert result["cluster_id"] == "prod-pg-1"
    assert result["proposed"]["min_acu"] == 1.0
    assert result["proposed"]["max_acu"] == 8.0
    assert "cost_impact" in result
    assert "current_monthly_estimate" in result["cost_impact"]
    assert "proposed_monthly_estimate" in result["cost_impact"]


def test_simulate_scaling_default_values():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)

    result = simulate_scaling_impl(mock_cache, cluster_id="prod-pg-1")

    assert result["proposed"]["min_acu"] == 0.5
    assert result["proposed"]["max_acu"] == 4.0
