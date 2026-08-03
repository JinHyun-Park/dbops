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


# ===== Serverless v2: the tier must come from the live max ACU =====

def _sv2_cache(max_acu, size_bytes=2 * 1024 * 1024 * 1024):
    """cluster_meta says db.serverless with the given max ACU; table_stats says
    `size_bytes`. The impl issues the meta query first, then the size query."""
    cache = MagicMock()
    cache.execute.side_effect = [
        QueryResult(
            columns=["instance_class", "engine", "serverlessv2_max_acu"],
            rows=[{"instance_class": "db.serverless",
                   "engine": "aurora-postgresql",
                   "serverlessv2_max_acu": max_acu}],
            row_count=1,
        ),
        _stats(size_bytes=size_bytes),
    ]
    return cache


def _seconds_for(max_acu):
    result = simulate_ddl_impact_impl(
        _sv2_cache(max_acu), cluster_id="sv2-cluster",
        ddl_sql="CREATE INDEX idx_orders_date ON orders (order_date)",
    )
    return result["estimated_seconds"], result


def test_small_serverless_v2_cluster_is_slower_than_a_large_one():
    """A 2-ACU dev cluster must not be estimated at a big cluster's throughput.

    The tier was a hardcoded mid-tier constant, so a 2-ACU cluster and a 128-ACU
    cluster got the SAME window: the small one understated by ~3-4x, in the
    direction that makes a maintenance window run over.
    """
    small_s, small = _seconds_for(2)
    large_s, _ = _seconds_for(128)
    assert small_s > large_s
    # And the basis must say which ACU figure was used, not just assert a number.
    assert any("2 ACU" in f for f in small["basis"]), small["basis"]


def test_serverless_v2_without_a_collected_acu_range_degrades():
    """No ACU collected yet is a data gap, not a reason to fail: keep the old
    assumption and SAY it is an assumption."""
    _, result = _seconds_for(None)
    assert result["estimated_seconds"] > 0
    assert any("수집되지 않아" in f for f in result["basis"]), result["basis"]


def test_acu_tier_is_monotonic_across_the_whole_range():
    """Throughput must never drop as ACU rises (a bucket-table typo would)."""
    from mcp_servers.shared.ddl_estimator import throughput_mb_s

    seen = [throughput_mb_s("db.serverless", False, acu)[0]
            for acu in (1, 4, 8, 16, 32, 64, 128, 192, 256, 1024)]
    assert seen == sorted(seen), seen
    assert seen[0] < seen[-1]
