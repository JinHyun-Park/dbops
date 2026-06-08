from unittest.mock import MagicMock

from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.ddl_impact import simulate_ddl_impact_impl


def test_simulate_online_ddl():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["relname", "row_count", "size_bytes"],
        rows=[{"relname": "orders", "row_count": 500000, "size_bytes": 104857600}],
        row_count=1,
    )

    result = simulate_ddl_impact_impl(
        mock_cache, cluster_id="prod-pg-1", ddl_sql="ALTER TABLE orders ADD INDEX idx_date (order_date)"
    )

    assert result["cluster_id"] == "prod-pg-1"
    assert result["table"] == "ORDERS"
    assert result["online_ddl_possible"] is True
    assert result["lock_type"] == "none (online)"
    assert result["disk_space_needed_mb"] > 0


def test_simulate_exclusive_lock_ddl():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)

    result = simulate_ddl_impact_impl(
        mock_cache, cluster_id="prod-pg-1", ddl_sql="ALTER TABLE users DROP COLUMN email"
    )

    assert result["online_ddl_possible"] is False
    assert result["lock_type"] == "exclusive"
    assert "점검" in result["recommendation"]


def test_ddl_impact_reads_cluster_scoped_cache_not_local_catalog():
    """Regression: table size/rows must come from the pre-collected
    `table_stats` cache filtered by cluster_id, not the cache DB's own
    pg_stat_user_tables."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    simulate_ddl_impact_impl(
        mock_cache, cluster_id="prod-pg-1", ddl_sql="ALTER TABLE orders ADD COLUMN note text"
    )
    sql, params = mock_cache.execute.call_args.args
    assert "table_stats" in sql
    assert "pg_stat_user_tables" not in sql
    assert "cluster_id = :cluster_id" in sql
    assert params["cluster_id"] == "prod-pg-1"
