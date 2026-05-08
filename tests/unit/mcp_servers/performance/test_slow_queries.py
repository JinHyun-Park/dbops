from unittest.mock import MagicMock
from mcp_servers.performance.tools.slow_queries import get_slow_queries_impl
from mcp_servers.shared.models import QueryResult


def test_get_slow_queries_filters_by_threshold():
    mock_cache = MagicMock()
    mock_cache._build_query.return_value = (
        "SELECT * FROM slow_queries WHERE cluster_id = :cluster_id AND execution_time_ms >= :threshold_ms ORDER BY execution_time_ms DESC LIMIT 20",
        {"cluster_id": "prod-pg-1"},
    )
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_slow_queries_impl(mock_cache, cluster_id="prod-pg-1", threshold_ms=500.0)
    call_args = mock_cache.execute.call_args
    assert "execution_time_ms >= :threshold_ms" in call_args[0][0]
    assert call_args[0][1]["threshold_ms"] == 500.0
