from unittest.mock import MagicMock
from mcp_servers.performance.tools.detect_regressions import detect_regressions_impl
from mcp_servers.shared.models import QueryResult


def test_detect_regressions_finds_degraded_queries():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["query_hash", "query_text", "before_mean_ms", "after_mean_ms", "change_pct"],
        rows=[{"query_hash": "abc", "query_text": "SELECT *", "before_mean_ms": 10.0, "after_mean_ms": 50.0, "change_pct": 400.0}],
        row_count=1,
    )
    result = detect_regressions_impl(mock_cache, cluster_id="prod-pg-1", change_point="2026-05-07T12:00:00Z")
    assert result["regressions"][0]["change_pct"] == 400.0
