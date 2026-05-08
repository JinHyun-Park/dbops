from unittest.mock import MagicMock
from mcp_servers.performance.tools.top_queries import get_top_queries_impl
from mcp_servers.shared.models import QueryResult


def test_get_top_queries_returns_sorted_results():
    mock_cache = MagicMock()
    mock_cache._build_query.return_value = (
        "SELECT * FROM query_stats WHERE cluster_id = :cluster_id ORDER BY total_time_ms DESC LIMIT 10",
        {"cluster_id": "prod-pg-1"},
    )
    mock_cache.execute.return_value = QueryResult(
        columns=["query_hash", "query_text", "calls", "total_time_ms", "mean_time_ms"],
        rows=[
            {"query_hash": "abc", "query_text": "SELECT * FROM orders", "calls": 100, "total_time_ms": 5000.0, "mean_time_ms": 50.0},
            {"query_hash": "def", "query_text": "SELECT * FROM users", "calls": 200, "total_time_ms": 3000.0, "mean_time_ms": 15.0},
        ],
        row_count=2,
    )
    result = get_top_queries_impl(mock_cache, cluster_id="prod-pg-1", sort_by="total_time", limit=10)
    assert result["row_count"] == 2
    assert len(result["queries"]) == 2
    mock_cache.execute.assert_called_once()


def test_get_top_queries_with_calls_sort():
    mock_cache = MagicMock()
    mock_cache._build_query.return_value = (
        "SELECT * FROM query_stats WHERE cluster_id = :cluster_id ORDER BY calls DESC LIMIT 5",
        {"cluster_id": "prod-pg-1"},
    )
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_top_queries_impl(mock_cache, cluster_id="prod-pg-1", sort_by="calls", limit=5)
    call_args = mock_cache.execute.call_args
    assert "calls DESC" in call_args[0][0]
