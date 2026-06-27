from unittest.mock import MagicMock

from mcp_servers.performance.tools.slow_queries import get_slow_queries_impl
from mcp_servers.shared.models import QueryResult


def test_get_slow_queries_derives_from_query_stats():
    """The dedicated slow_queries table is never populated by any collector, so
    the tool must derive from query_stats by mean execution time (like the
    dashboard). Assert the REAL _build_query arguments, not a mocked SQL string —
    a mocked return value would hide a regression back to the dead table."""
    mock_cache = MagicMock()
    mock_cache._build_query.return_value = ("SELECT 1", {"cluster_id": "prod-pg-1"})
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)

    get_slow_queries_impl(mock_cache, cluster_id="prod-pg-1", threshold_ms=500.0)

    kwargs = mock_cache._build_query.call_args.kwargs
    assert kwargs["table"] == "query_stats", "must not query the unpopulated slow_queries table"
    assert kwargs["time_column"] == "snapshot_time"
    assert "mean_time_ms >= :threshold_ms" in kwargs["extra_where"]
    assert "mean_time_ms" in kwargs["order_by"]
    # threshold is bound into the executed params
    assert mock_cache.execute.call_args[0][1]["threshold_ms"] == 500.0
