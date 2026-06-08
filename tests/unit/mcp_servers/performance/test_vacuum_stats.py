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


def test_vacuum_stats_reads_cluster_scoped_cache_not_local_catalog():
    """Regression: must query the pre-collected `table_stats` cache filtered by
    cluster_id — NOT the cache DB's own pg_stat_user_tables (which would report
    DBOps' internal tables for every cluster)."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    get_vacuum_stats_impl(mock_cache, cluster_id="prod-pg-1")
    sql, params = mock_cache.execute.call_args.args
    assert "table_stats" in sql
    assert "pg_stat_user_tables" not in sql
    assert "cluster_id = :cluster_id" in sql
    assert params["cluster_id"] == "prod-pg-1"
