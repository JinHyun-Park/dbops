from unittest.mock import MagicMock

from mcp_servers.performance.tools.forecast_capacity import forecast_capacity_impl
from mcp_servers.shared.models import QueryResult


def test_forecast_capacity_returns_projection():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["slope_per_day", "current_value", "max_value"],
        rows=[{"slope_per_day": 2.5, "current_value": 180.0, "max_value": 500.0}],
        row_count=1,
    )
    result = forecast_capacity_impl(mock_cache, cluster_id="prod-pg-1", metric="storage_gb")
    assert "days_until_limit" in result
    assert result["current_value"] == 180.0
