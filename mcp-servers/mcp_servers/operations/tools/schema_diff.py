from mcp_servers.shared.cache_client import CacheClient


def get_schema_diff_impl(cache: CacheClient, cluster_id: str, snapshot_a: str = None, snapshot_b: str = None) -> dict:
    if snapshot_a and snapshot_b:
        sql = """
            SELECT a.schema_name, a.tables_json as tables_before, b.tables_json as tables_after
            FROM schema_snapshots a, schema_snapshots b
            WHERE a.cluster_id = :cluster_id AND b.cluster_id = :cluster_id
              AND a.snapshot_time = :snapshot_a::timestamptz AND b.snapshot_time = :snapshot_b::timestamptz
              AND a.schema_name = b.schema_name
        """
        params = {"cluster_id": cluster_id, "snapshot_a": snapshot_a, "snapshot_b": snapshot_b}
    else:
        sql = """
            SELECT schema_name, diff_from_previous_json as diff
            FROM schema_snapshots
            WHERE cluster_id = :cluster_id
            ORDER BY snapshot_time DESC LIMIT 1
        """
        params = {"cluster_id": cluster_id}
    result = cache.execute(sql, params)
    return {"cluster_id": cluster_id, "diffs": result.rows, "count": result.row_count}
