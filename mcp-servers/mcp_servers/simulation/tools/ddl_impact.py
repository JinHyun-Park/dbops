from mcp_servers.shared.cache_client import CacheClient


def simulate_ddl_impact_impl(cache: CacheClient, cluster_id: str, ddl_sql: str) -> dict:
    ddl_upper = ddl_sql.strip().upper()

    table_match = None
    for keyword in ("ALTER TABLE ", "CREATE INDEX ON ", "DROP TABLE ", "DROP INDEX "):
        if keyword in ddl_upper:
            parts = ddl_upper.split(keyword)[1].split()
            table_match = parts[0].strip("(;") if parts else None
            break

    table_info = {}
    if table_match:
        # Read the pre-collected `table_stats` cache (scoped to this cluster,
        # latest snapshot for the named table) rather than the cache DB's own
        # pg_stat_user_tables — the previous version had no cluster filter and
        # introspected the DBOps cache instead of the target cluster.
        info_sql = """
            SELECT table_name, n_live_tup AS row_count, total_bytes AS size_bytes
            FROM table_stats
            WHERE cluster_id = :cluster_id AND upper(table_name) = :table_name
            ORDER BY snapshot_time DESC
            LIMIT 1
        """
        result = cache.execute(info_sql, {"cluster_id": cluster_id, "table_name": table_match})
        table_info = result.rows[0] if result.rows else {}

    row_count = int(table_info.get("row_count", 0))
    size_bytes = int(table_info.get("size_bytes", 0))
    size_mb = size_bytes / (1024 * 1024) if size_bytes else 0

    estimated_seconds = max(1, row_count / 100000) * 5
    online_ddl = "ADD COLUMN" in ddl_upper or "ADD INDEX" in ddl_upper or "CREATE INDEX" in ddl_upper
    lock_type = "none (online)" if online_ddl else "exclusive"

    return {
        "cluster_id": cluster_id,
        "ddl": ddl_sql,
        "table": table_match or "unknown",
        "table_info": {"rows": row_count, "size_mb": round(size_mb, 1)},
        "estimated_seconds": round(estimated_seconds),
        "online_ddl_possible": online_ddl,
        "lock_type": lock_type,
        "disk_space_needed_mb": round(size_mb * 1.5, 1) if "INDEX" in ddl_upper else 0,
        "recommendation": "온라인 DDL 가능 — 서비스 영향 없음" if online_ddl else "배타적 락 필요 — 점검 윈도우에서 수행 권장",
    }
