from unittest.mock import MagicMock

from mcp_servers.performance.tools.vacuum_stats import get_vacuum_stats_impl
from mcp_servers.shared.models import QueryResult


def test_vacuum_stats_detects_bloat():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["table_name", "dead_tuples", "live_tuples", "bloat_pct"],
        rows=[
            {"table_name": "orders", "dead_tuples": 50000, "live_tuples": 100000, "bloat_pct": 50.0},
            {"table_name": "users", "dead_tuples": 100, "live_tuples": 10000, "bloat_pct": 1.0},
        ],
        row_count=2,
    )
    result = get_vacuum_stats_impl(mock_cache, cluster_id="prod-pg-1")
    assert len(result["warnings"]) == 1
    assert "orders" in result["warnings"][0]
