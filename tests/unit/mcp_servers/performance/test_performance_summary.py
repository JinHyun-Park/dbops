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


def test_slow_count_derives_from_query_stats_not_dead_table():
    """slow_count must come from query_stats (distinct queries with high mean
    time), not the unpopulated slow_queries table which always returned 0."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    get_performance_summary_impl(mock_cache, cluster_id="prod-pg-1", hours=24)
    sql = mock_cache.execute.call_args[0][0]
    assert "FROM slow_queries" not in sql, "must not query the unpopulated slow_queries table"
    assert "COUNT(DISTINCT query_hash) FROM query_stats" in sql
    assert "mean_time_ms >= 1000" in sql
