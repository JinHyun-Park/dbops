from unittest.mock import MagicMock
from mcp_servers.performance.tools.compare_periods import compare_periods_impl
from mcp_servers.shared.models import QueryResult


def test_compare_periods_calls_twice():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["avg_value", "max_value", "min_value", "sample_count"],
        rows=[{"avg_value": 3.5, "max_value": 8.0, "min_value": 0.5, "sample_count": 100}],
        row_count=1,
    )
    result = compare_periods_impl(
        mock_cache, "prod-pg-1",
        "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
        "2026-05-07T00:00:00Z", "2026-05-08T00:00:00Z",
    )
    assert mock_cache.execute.call_count == 2
    assert "period_a" in result
    assert "period_b" in result
