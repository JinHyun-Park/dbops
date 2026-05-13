from unittest.mock import MagicMock

from mcp_servers.performance.tools.recommend_index import recommend_index_impl
from mcp_servers.shared.models import QueryResult


def test_recommend_index_returns_recommendations():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["query_hash", "query_text", "total_time_ms", "calls", "index_scans", "blocks_read"],
        rows=[{"query_hash": "abc", "query_text": "SELECT * FROM orders WHERE status = 'pending'", "total_time_ms": 5000, "calls": 100, "index_scans": 0, "blocks_read": 5000}],
        row_count=1,
    )
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 1
    assert "missing" in result["recommendations"][0]["reason"].lower()
