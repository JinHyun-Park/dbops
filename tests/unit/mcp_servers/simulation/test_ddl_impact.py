from unittest.mock import MagicMock

from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.ddl_impact import simulate_ddl_impact_impl


def _stats(row_count=500000, size_bytes=104857600):
    return QueryResult(
        columns=["table_name", "row_count", "size_bytes"],
        rows=[{"table_name": "orders", "row_count": row_count, "size_bytes": size_bytes}],
        row_count=1,
    )


def test_create_index_concurrently_is_online_and_size_driven():
    """CONCURRENTLY index build is non-blocking and its time scales with size."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _stats(size_bytes=2 * 1024 * 1024 * 1024)  # 2 GB
    result = simulate_ddl_impact_impl(
        mock_cache, cluster_id="prod-pg-1",
        ddl_sql="CREATE INDEX CONCURRENTLY idx_orders_date ON orders (order_date)",
    )
    assert result["operation"] == "create_index"
    assert result["online_ddl_possible"] is True
    assert "CONCURRENTLY" in result["lock_type"]
    assert result["disk_space_needed_mb"] > 0
    # 2GB scan at ~40MB/s, ×2 for concurrent → well over the 5s floor
    assert result["estimated_seconds"] > 60


def test_plain_create_index_is_blocking():
    """A non-CONCURRENTLY index build blocks writes (the old code wrongly called
    every CREATE INDEX online)."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _stats()
    result = simulate_ddl_impact_impl(
        mock_cache, cluster_id="prod-pg-1",
        ddl_sql="CREATE INDEX idx_orders_date ON orders (order_date)",
    )
    assert result["operation"] == "create_index"
    assert result["online_ddl_possible"] is False
    assert "blocking" in result["lock_type"]


def test_drop_column_is_metadata_only():
    """DROP COLUMN is metadata-only — near-instant regardless of table size,
    and size-independent (empty stats still returns the metadata floor)."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = simulate_ddl_impact_impl(
        mock_cache, cluster_id="prod-pg-1", ddl_sql="ALTER TABLE users DROP COLUMN email"
    )
    assert result["operation"] == "drop_column"
    assert result["online_ddl_possible"] is False
    assert result["estimated_seconds"] <= 5  # metadata-only floor
    assert "점검" in result["recommendation"]


def test_add_column_is_metadata_only_online():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _stats()
    result = simulate_ddl_impact_impl(
        mock_cache, cluster_id="prod-pg-1", ddl_sql="ALTER TABLE orders ADD COLUMN note text"
    )
    assert result["operation"] == "add_column"
    assert result["online_ddl_possible"] is True
    # metadata-only: time does not scale with the 100MB table
    assert result["estimated_seconds"] <= 5


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


def test_add_column_with_default_is_conservative_not_online():
    """ADD COLUMN with a DEFAULT (possibly volatile) or GENERATED may rewrite the
    table — it must NOT be flagged online (the dangerous mis-flag Codex caught)."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _stats()
    result = simulate_ddl_impact_impl(
        mock_cache, cluster_id="prod-pg-1",
        ddl_sql="ALTER TABLE orders ADD COLUMN created_at timestamptz DEFAULT now()",
    )
    assert result["operation"] == "add_column"
    assert result["online_ddl_possible"] is False
    assert "rewrite" in result["lock_type"].lower()
