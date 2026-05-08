from mcp_servers.shared.cache_client import CacheClient


def test_build_query_with_cluster_filter():
    client = CacheClient.__new__(CacheClient)
    sql, params = client._build_query(
        table="query_stats",
        cluster_id="prod-pg-1",
        time_column="snapshot_time",
        start_time="2026-05-01T00:00:00Z",
        end_time="2026-05-02T00:00:00Z",
        order_by="total_time_ms DESC",
        limit=10,
    )
    assert "query_stats" in sql
    assert "cluster_id" in sql
    assert "snapshot_time >=" in sql
    assert "ORDER BY total_time_ms DESC" in sql
    assert "LIMIT 10" in sql
    assert params["cluster_id"] == "prod-pg-1"


def test_build_query_without_time_range():
    client = CacheClient.__new__(CacheClient)
    sql, params = client._build_query(
        table="cluster_meta",
        cluster_id="prod-pg-1",
    )
    assert "cluster_meta" in sql
    assert "snapshot_time" not in sql
