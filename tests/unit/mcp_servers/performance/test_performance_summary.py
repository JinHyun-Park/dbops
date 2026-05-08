from unittest.mock import MagicMock
from mcp_servers.performance.tools.performance_summary import get_performance_summary_impl
from mcp_servers.shared.models import QueryResult


def test_performance_summary_returns_kpis():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["avg_aas", "max_aas", "slow_count", "peak_connections"],
        rows=[{"avg_aas": 3.2, "max_aas": 12.5, "slow_count": 47, "peak_connections": 195}],
        row_count=1,
    )
    result = get_performance_summary_impl(mock_cache, cluster_id="prod-pg-1", hours=24)
    assert result["kpis"]["avg_aas"] == 3.2
    assert result["kpis"]["slow_count"] == 47
