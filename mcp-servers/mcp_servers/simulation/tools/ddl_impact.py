"""ddl_impact — estimate the lock + time footprint of a DDL statement.

WHY this shape: the table SIZE/ROWS come from the pre-collected `table_stats`
cache (real per-cluster data — see the cluster-scoped query below). The lock
behaviour and runtime are then derived from the OPERATION CLASS, not a single
magic formula:

  - Metadata-only ops (DROP COLUMN, ADD COLUMN with a constant/!default, DROP
    INDEX/TABLE) are effectively instant in modern PostgreSQL/MySQL regardless
    of table size — only a brief catalog lock.
  - Full-scan ops (CREATE INDEX, ALTER COLUMN ... TYPE, table rewrites) read the
    whole table, so runtime scales with size; runtime ~ size_mb / throughput.
  - Online vs blocking depends on the exact syntax: a plain `CREATE INDEX` takes
    a write-blocking lock, while `CREATE INDEX CONCURRENTLY` does not (and runs
    ~2x longer). The previous version wrongly called every CREATE INDEX "online".

The runtime is an ESTIMATE with documented assumptions (throughput constant),
surfaced in the returned `note` — it is directional guidance, not a guarantee.
"""

import re

from mcp_servers.shared.cache_client import CacheClient

# Assumed sustained throughput (MB/s) for a full-table scan/rewrite on Aurora.
# WHY a constant: real throughput depends on instance size, IO, cache state and
# concurrent load; ~40 MB/s is a conservative ballpark for directional estimates.
_SCAN_THROUGHPUT_MB_S = 40.0
# Metadata-only operations are near-instant; we still report a small floor to
# account for catalog lock acquisition + plan invalidation.
_METADATA_ONLY_SECONDS = 3
# CONCURRENTLY index builds avoid the write lock but do ~2 passes over the table.
_CONCURRENT_SLOWDOWN = 2.0


def _classify(ddl_upper: str) -> dict:
    """Classify the DDL into (operation, scans_table, online, lock) using the
    statement shape. Engine-pragmatic: covers the common Aurora PG/MySQL DDL the
    agent is asked to simulate, and falls back to a conservative blocking
    estimate for anything unrecognized."""
    has_concurrently = "CONCURRENTLY" in ddl_upper

    if "CREATE INDEX" in ddl_upper or "ADD INDEX" in ddl_upper:
        # Index build always scans the table. Only CONCURRENTLY is non-blocking.
        return {
            "operation": "create_index",
            "scans_table": True,
            "online": has_concurrently,
            "lock": "none (CONCURRENTLY)" if has_concurrently else "blocking (writes blocked during build)",
        }
    if "ALTER COLUMN" in ddl_upper and "TYPE" in ddl_upper:
        # Type change rewrites the whole table under ACCESS EXCLUSIVE.
        return {"operation": "alter_column_type", "scans_table": True, "online": False, "lock": "exclusive (table rewrite)"}
    if "ADD COLUMN" in ddl_upper:
        # PG11+/MySQL 8: adding a column with NO default is a metadata-only
        # catalog change (instant, online). But a DEFAULT (which may be volatile,
        # e.g. now()/uuid) or a GENERATED column CAN rewrite the whole table —
        # and we can't prove the default is a constant from text alone. So be
        # CONSERVATIVE: only flag online when there is no DEFAULT/GENERATED;
        # otherwise treat it as a potential blocking rewrite. (online=True on a
        # rewriting ADD COLUMN would be the dangerous, misleading case.)
        if "DEFAULT" in ddl_upper or "GENERATED" in ddl_upper:
            return {
                "operation": "add_column",
                "scans_table": True,
                "online": False,
                "lock": "potentially blocking — DEFAULT/GENERATED may rewrite the table",
            }
        return {"operation": "add_column", "scans_table": False, "online": True, "lock": "brief (metadata-only — no default)"}
    if "DROP COLUMN" in ddl_upper:
        return {"operation": "drop_column", "scans_table": False, "online": False, "lock": "exclusive but metadata-only (fast)"}
    if "DROP INDEX" in ddl_upper:
        return {"operation": "drop_index", "scans_table": False, "online": False, "lock": "brief exclusive"}
    if "DROP TABLE" in ddl_upper or "TRUNCATE" in ddl_upper:
        return {"operation": "drop_or_truncate", "scans_table": False, "online": False, "lock": "exclusive but metadata-only (fast)"}
    # Unknown DDL: be conservative — assume a blocking, size-proportional op.
    return {"operation": "other", "scans_table": True, "online": False, "lock": "exclusive (assumed — unrecognized DDL)"}


def simulate_ddl_impact_impl(cache: CacheClient, cluster_id: str, ddl_sql: str) -> dict:
    ddl_upper = ddl_sql.strip().upper()

    # Resolve the target table. ALTER TABLE / DROP TABLE name the table directly;
    # CREATE INDEX names it after `ON` (after the optional CONCURRENTLY + index
    # name), so a naive "CREATE INDEX ON" split misses `CREATE INDEX [CONCURRENTLY]
    # <name> ON <table>`. DROP INDEX names an index, not a table (no size lookup).
    table_match = None
    if "ALTER TABLE " in ddl_upper:
        parts = ddl_upper.split("ALTER TABLE ", 1)[1].split()
        table_match = parts[0].strip("(;") if parts else None
    elif "CREATE INDEX" in ddl_upper:
        m = re.search(r"\bON\s+([A-Za-z_][\w$.]*)", ddl_upper)
        table_match = m.group(1).strip("(;") if m else None
    elif "DROP TABLE " in ddl_upper:
        parts = ddl_upper.split("DROP TABLE ", 1)[1].split()
        table_match = parts[0].strip("(;") if parts else None

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

    row_count = int(table_info.get("row_count", 0) or 0)
    size_bytes = int(table_info.get("size_bytes", 0) or 0)
    size_mb = size_bytes / (1024 * 1024) if size_bytes else 0

    cls = _classify(ddl_upper)

    # Runtime model: full-scan ops scale with table size; metadata-only ops are
    # near-instant. CONCURRENTLY index builds run ~2x.
    if cls["scans_table"]:
        estimated_seconds = max(5.0, size_mb / _SCAN_THROUGHPUT_MB_S)
        if "CONCURRENTLY" in ddl_upper:
            estimated_seconds *= _CONCURRENT_SLOWDOWN
        time_basis = f"~{size_mb:.0f}MB / {_SCAN_THROUGHPUT_MB_S:.0f}MB·s⁻¹ full scan"
    else:
        estimated_seconds = _METADATA_ONLY_SECONDS
        time_basis = "metadata-only (size-independent)"

    # An index needs disk for the new index; estimate from table size as an
    # upper bound (the index covers a subset of columns, so this over-estimates).
    disk_needed_mb = round(size_mb * 0.5, 1) if cls["operation"] == "create_index" else 0

    return {
        "cluster_id": cluster_id,
        "ddl": ddl_sql,
        "table": table_match or "unknown",
        "operation": cls["operation"],
        "table_info": {"rows": row_count, "size_mb": round(size_mb, 1)},
        "estimated_seconds": round(estimated_seconds),
        "online_ddl_possible": cls["online"],
        "lock_type": cls["lock"],
        "disk_space_needed_mb": disk_needed_mb,
        "recommendation": (
            "온라인 DDL 가능 — 서비스 영향 최소" if cls["online"]
            else "쓰기 차단/배타적 락 — 점검 윈도우에서 수행 권장"
        ),
        "note": (
            f"런타임은 추정치입니다 ({time_basis}; 가정 처리량 {_SCAN_THROUGHPUT_MB_S:.0f}MB/s). "
            "ADD COLUMN은 상수/무default일 때만 메타데이터 변경이며, volatile default는 테이블 재작성을 "
            "유발합니다. 실제 시간은 인스턴스 크기·동시 부하·캐시 상태에 따라 달라집니다."
            if cls["scans_table"] or cls["operation"] == "add_column"
            else "메타데이터 전용 작업으로 테이블 크기와 무관하게 거의 즉시 완료됩니다."
        ),
    }
