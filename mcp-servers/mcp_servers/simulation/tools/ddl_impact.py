"""ddl_impact — estimate the lock + time footprint of a DDL statement.

Thin wrapper over the shared :mod:`ddl_estimator` model. This tool's job is to
resolve the REAL signals from the cache — the target table's size/rows from the
pre-collected ``table_stats`` (cluster-scoped, latest snapshot) and the
cluster's ``instance_class`` from ``cluster_meta`` — and hand them to the shared
estimator, which derives throughput from the instance class (not a hardcoded
constant) and returns a range + confidence + the factors used. The MCP tool and
the REST mirror share that estimator so they can never drift.
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.ddl_estimator import estimate_ddl, resolve_table


def simulate_ddl_impact_impl(cache: CacheClient, cluster_id: str, ddl_sql: str) -> dict:
    table = resolve_table(ddl_sql)

    # Instance class grounds the throughput estimate. I/O-Optimized isn't in
    # cluster_meta, so default to Standard (conservative — slower) and let the
    # note flag it; a live describe would refine it but adds latency/perms.
    # Queried BEFORE the table_stats lookup so the size query stays the last
    # cache call (it carries the cluster-scoped contract the tests pin).
    meta = cache.execute(
        "SELECT instance_class FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    instance_class = meta.rows[0].get("instance_class") if meta.rows else None

    row_count = 0
    size_mb = 0.0
    if table:
        # Read the pre-collected `table_stats` cache (scoped to this cluster,
        # latest snapshot for the named table) — NOT the cache DB's own
        # pg_stat_user_tables.
        info_sql = """
            SELECT n_live_tup AS row_count, total_bytes AS size_bytes
            FROM table_stats
            WHERE cluster_id = :cluster_id AND lower(table_name) = :table_name
            ORDER BY snapshot_time DESC
            LIMIT 1
        """
        result = cache.execute(info_sql, {"cluster_id": cluster_id, "table_name": table.lower()})
        row = result.rows[0] if result.rows else {}
        row_count = int(row.get("row_count") or 0)
        size_bytes = int(row.get("size_bytes") or 0)
        size_mb = size_bytes / (1024 * 1024) if size_bytes else 0.0

    est = estimate_ddl(
        ddl_sql=ddl_sql,
        table=table,
        row_count=row_count,
        size_mb=size_mb,
        instance_class=instance_class,
        io_optimized=False,
    )
    return {"cluster_id": cluster_id, **est}
