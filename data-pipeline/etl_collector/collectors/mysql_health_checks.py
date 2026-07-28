"""MySQL Maintenance Health checks: the MySQL counterpart of pg_health_checks.py.

pg_health_checks is called ONLY from the "postgresql" in engine branch, so the
MySQL branch had no health-check collector at all. This closes the part of that
gap that is real for InnoDB, and deliberately not the rest.

WHAT THIS EMITS (2 check_types, not the 7 the PG collector has):

  - mysql_fragmentation  : reclaimable free space inside the tablespace
  - setting_misconfigured: observability / durability parameters away from a
                           defensible value (same check_type as PG, so it lands
                           in the existing "Config" tab)

WHAT IT DELIBERATELY DOES NOT EMIT, and why:

  - txid_age, vacuum_overdue, extension_missing: inherently non-applicable.
    InnoDB has no visibility-map freeze, no autovacuum, and plugins are not
    CREATE EXTENSION.
  - dead_tuples: on MySQL this would be THE SAME NUMBER as fragmentation.
    table_stats.n_dead_tup is FLOOR(DATA_FREE / AVG_ROW_LENGTH), and both PG
    checks (dead_tuples and table_bloat) derive from that one quantity. Emitting
    both reports one signal twice, under a PG name InnoDB does not have.
  - index_unused: the cache has no per-index granularity (mysql_table_stats
    GROUP BYs per-index COUNT_FETCH into one table-level idx_scan), and
    performance_schema.table_io_waits_summary_by_index_usage was measured EMPTY
    for the live sampledb schema. A naive check over that source reports every
    index as unused. The answer already ships for Aurora MySQL through
    api/dashboard/handler.py::_redundant_indexes and RedundantIndexesPanel, which
    queries the target directly at the right granularity.

Reads the CACHE ONLY (table_stats from mysql_table_stats, cluster_settings from
mysql_locks), never the live cluster, the same pattern as mysql_param_fitness.
"""

import json
from datetime import datetime, timezone

# Same Data-API row reader mysql_param_fitness uses (including the `or label`
# column-name fallback). Imported rather than copied: a second divergent copy of
# this helper is how a column-name regression hides in one collector but not the
# other.
from collectors.mysql_param_fitness import _execute

# --- fragmentation thresholds -------------------------------------------------
# NOT PG's BLOAT_WARN_PCT of 20. Reclaimable space in InnoDB is partly normal:
# the free list holds pages for reuse, and reclaiming it means OPTIMIZE TABLE,
# which rebuilds the entire table under a metadata lock. Measured on the live
# Aurora MySQL demo cluster: products 11.26%, sales 9.27%. That is ordinary churn
# and must NOT raise a warning, so the bar sits above it.
FRAGMENTATION_WARN_PCT = 25.0
FRAGMENTATION_CRITICAL_PCT = 40.0
# Below this the absolute reclaimable space is small and a rebuild is cheap, so a
# finding would be noise. Both live tables (963,662 and 1,284,750 rows) are above
# it, so the threshold above is the only thing keeping them silent.
FRAGMENTATION_MIN_LIVE_ROWS = 100_000
# How many tables to report, worst first.
FRAGMENTATION_MAX_TABLES = 10

# MySQL's own default long_query_time. At the default, only queries slower than
# 10 seconds reach the slow query log, which misses most of what a DBA is
# hunting.
LONG_QUERY_TIME_CEILING = 10.0


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def collect_mysql_health_checks(
    rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id,
    snapshot_ts=None,
):
    """Emit MySQL Maintenance Health findings into cluster_health_findings.

    snapshot_ts: the shared per-run timestamp from the ETL handler. Every finding
    in one cycle MUST share it, or the dashboard's MAX(snapshot_time) query shows
    only the last batch written (the bug this collector family has hit before).
    """
    ts = snapshot_ts or datetime.now(timezone.utc).isoformat()
    findings = []

    def add(check_type, severity, subject, value_str, threshold_str, recommendation, details):
        findings.append({
            "check_type": check_type, "severity": severity, "subject": subject,
            "value_str": value_str, "threshold_str": threshold_str,
            "recommendation": recommendation, "details": json.dumps(details),
        })

    # --- 1) Fragmentation (reclaimable free space) ---------------------------
    # n_dead_tup is FLOOR(DATA_FREE / AVG_ROW_LENGTH) and n_live_tup is
    # TABLE_ROWS, so the ratio approximates DATA_FREE / DATA_LENGTH: free space as
    # a share of the stored data. Both inputs are information_schema ESTIMATES for
    # InnoDB, which is why the thresholds are coarse.
    table_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT DISTINCT ON (schema_name, table_name) "
        "       schema_name, table_name, n_live_tup, n_dead_tup, total_bytes "
        "FROM table_stats "
        "WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '24 hours' "
        "ORDER BY schema_name, table_name, snapshot_time DESC",
        {"cid": cluster_id},
    )
    # No rows means the deep-read collector has not run (or the window is empty).
    # Emit nothing: "no fragmentation finding" must never come from "no data".
    candidates = []
    for row in table_rows:
        live = _f(row.get("n_live_tup")) or 0.0
        free = _f(row.get("n_dead_tup")) or 0.0
        if live < FRAGMENTATION_MIN_LIVE_ROWS:
            continue
        candidates.append((free / live * 100.0, row, free, live))
    candidates.sort(key=lambda c: c[0], reverse=True)

    for pct, row, free, live in candidates[:FRAGMENTATION_MAX_TABLES]:
        if pct < FRAGMENTATION_WARN_PCT:
            continue
        subject = f"{row.get('schema_name')}.{row.get('table_name')}"
        severity = "warning" if pct < FRAGMENTATION_CRITICAL_PCT else "critical"
        bar = (FRAGMENTATION_WARN_PCT if severity == "warning"
               else FRAGMENTATION_CRITICAL_PCT)
        add(
            "mysql_fragmentation", severity, subject,
            f"재사용 가능 여유 공간 ≈ {pct:.1f}%",
            f"< {bar:.0f}%",
            f"{subject}의 테이블스페이스에 데이터 대비 약 {pct:.1f}%"
            f"(약 {int(free):,}행 분량)의 여유 공간이 잡혀 있습니다. InnoDB는 삭제된 행의 "
            f"공간을 free list에 두고 재사용하므로 일정 수준은 정상이지만, 이 비율이면 "
            f"OPTIMIZE TABLE로 테이블을 재구축해 공간을 회수할 여지가 있습니다. "
            f"OPTIMIZE TABLE은 테이블 전체를 다시 쓰고 메타데이터 락을 잡으므로 점검 "
            f"윈도우에서 실행하세요. 이 수치는 information_schema의 추정값입니다"
            f"(DATA_FREE / AVG_ROW_LENGTH).",
            {"schema": row.get("schema_name"), "table": row.get("table_name"),
             "pct": round(pct, 1), "free_rows_est": int(free),
             "live_rows_est": int(live), "total_bytes": row.get("total_bytes"),
             "precision": "estimate"},
        )

    # --- 2) Observability / durability settings -----------------------------
    # Source: cluster_settings, refreshed every cycle by mysql_locks' SETTINGS_SQL
    # (performance_schema.global_variables). A setting that is absent is SKIPPED,
    # never assumed: we only ever compare a value we actually read.
    setting_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT name, value FROM cluster_settings WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    settings = {r["name"]: r["value"] for r in setting_rows}

    slow_log = str(settings.get("slow_query_log") or "").strip().upper()
    long_query_time = _f(settings.get("long_query_time"))

    if slow_log in ("OFF", "0"):
        add(
            "setting_misconfigured", "warning", "slow_query_log",
            "current=OFF", "recommended=ON",
            "슬로우 쿼리 로그가 꺼져 있어 느린 쿼리가 CloudWatch Logs로 나가지 "
            "않습니다. 인시던트 조사에서 search_logs로 슬로우 쿼리를 확인할 수 없고, "
            "사후에 소급해 볼 수도 없습니다. slow_query_log를 ON으로 두고 "
            "long_query_time을 워크로드에 맞게(보통 1초) 낮추세요.",
            {"setting": "slow_query_log", "current": settings.get("slow_query_log"),
             "recommended": "ON"},
        )
    elif slow_log in ("ON", "1") and long_query_time is not None and long_query_time >= LONG_QUERY_TIME_CEILING:
        add(
            "setting_misconfigured", "warning", "long_query_time",
            f"current={long_query_time:g}s", f"< {LONG_QUERY_TIME_CEILING:g}s",
            f"슬로우 쿼리 로그는 켜져 있지만 long_query_time이 {long_query_time:g}초"
            f"(MySQL 기본값)라서 그보다 빠른 쿼리는 전혀 기록되지 않습니다. 실제 문제가 "
            f"되는 쿼리는 대부분 이 밑에 있으므로, 로그가 비어 있는 것을 '느린 쿼리가 "
            f"없다'로 읽으면 안 됩니다. 워크로드에 맞게(보통 1초) 낮추세요.",
            {"setting": "long_query_time", "current": long_query_time,
             "recommended_below": LONG_QUERY_TIME_CEILING},
        )

    flush_trx = str(settings.get("innodb_flush_log_at_trx_commit") or "").strip()
    if flush_trx and flush_trx != "1":
        add(
            "setting_misconfigured", "warning", "innodb_flush_log_at_trx_commit",
            f"current={flush_trx}", "recommended=1",
            f"innodb_flush_log_at_trx_commit가 {flush_trx}입니다. 1이 아니면 커밋이 "
            f"redo 로그 지속성을 보장하지 않으므로, 장애 시 마지막 약 1초 분량의 커밋을 "
            f"잃을 수 있습니다. 처리량을 위해 의도적으로 완화한 설정이라면 그 트레이드오프를 "
            f"문서화하고, 아니라면 1로 되돌리세요.",
            {"setting": "innodb_flush_log_at_trx_commit", "current": flush_trx,
             "recommended": "1"},
        )

    # NOTE: log_bin=OFF is intentionally NOT flagged. On Aurora MySQL binlog is
    # off by default and replication runs through the storage layer, so calling
    # it a misconfiguration would be wrong for the majority of clusters.

    for f in findings:
        _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "INSERT INTO cluster_health_findings "
            "(cluster_id, snapshot_time, check_type, severity, subject, value_str, "
            " threshold_str, recommendation, details) "
            "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, "
            " :value_str, :threshold_str, :recommendation, :details::jsonb)",
            {"cluster_id": cluster_id, "ts": ts, "check_type": f["check_type"],
             "severity": f["severity"], "subject": f["subject"],
             "value_str": f["value_str"], "threshold_str": f["threshold_str"],
             "recommendation": f["recommendation"], "details": f["details"]},
        )

    return {
        "cluster_id": cluster_id,
        "tables_examined": len(table_rows),
        "tables_over_min_rows": len(candidates),
        "settings_read": len(settings),
        "findings_emitted": len(findings),
    }
