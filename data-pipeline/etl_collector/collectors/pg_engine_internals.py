"""PG engine-internal stats → metric_snapshots + cluster_health_findings.

Surfaces signals the CloudWatch metrics (cw_collector) don't expose:
  - pg_stat_database: shared-buffers cache hit ratio, transaction rollback ratio,
    cumulative temp-file spill (work_mem pressure).
  - pg_stat_bgwriter: how many checkpoints were *forced* (req) vs scheduled
    (timed) — a high forced ratio means max_wal_size pressure.

All metrics are point-in-time gauges/ratios computed from a single source query,
so neither the collector nor the findings need a previous snapshot. Findings are
threshold checks on the just-computed values (no delta). Every query is wrapped
so a partial failure (e.g. pg_stat_bgwriter renamed to pg_stat_checkpointer on
PG 17) never drops the rest — mirrors the never-raises contract of the other
collectors.
"""

# pg_stat_database aggregated across user databases (templates excluded). The
# ratios are NULL-safe so an idle cluster with zero reads doesn't divide by zero.
DB_STATS_SQL = """
SELECT
  100.0 * sum(blks_hit) / NULLIF(sum(blks_hit) + sum(blks_read), 0) AS cache_hit_ratio,
  100.0 * sum(xact_rollback) / NULLIF(sum(xact_commit) + sum(xact_rollback), 0) AS rollback_ratio,
  sum(temp_bytes) AS temp_bytes
FROM pg_stat_database
WHERE datname NOT IN ('template0', 'template1')
"""

# pg_stat_bgwriter is a single cluster-wide row (≤ PG16; PG17 moved checkpoints
# to pg_stat_checkpointer — that failure is caught and skipped).
BGWRITER_SQL = """
SELECT 100.0 * checkpoints_req / NULLIF(checkpoints_req + checkpoints_timed, 0)
         AS forced_checkpoint_ratio
FROM pg_stat_bgwriter
"""

INSERT_METRIC = (
    "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
    "VALUES (:cluster_id, NOW(), :metric_type, :value, '{}'::jsonb) "
    "ON CONFLICT DO NOTHING"
)

INSERT_FINDING = (
    "INSERT INTO cluster_health_findings "
    "(cluster_id, snapshot_time, check_type, severity, subject, value_str, "
    " threshold_str, recommendation, details) "
    "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, "
    " :value_str, :threshold_str, :recommendation, :details::jsonb)"
)


def _double(field):
    return field.get("doubleValue", 0.0) if not field.get("isNull") else 0.0


def collect_pg_engine_internals(
    rds_data_client, cache_execute, target_cluster_arn, target_secret_arn,
    cluster_id, database, snapshot_ts,
):
    inserted = 0
    findings = 0
    errors = []

    def metric(metric_type, value):
        nonlocal inserted
        cache_execute(INSERT_METRIC, {
            "cluster_id": cluster_id, "metric_type": metric_type, "value": float(value),
        })
        inserted += 1

    def finding(check_type, severity, subject, value_str, threshold_str, recommendation):
        nonlocal findings
        cache_execute(INSERT_FINDING, {
            "cluster_id": cluster_id, "ts": snapshot_ts, "check_type": check_type,
            "severity": severity, "subject": subject, "value_str": value_str,
            "threshold_str": threshold_str, "recommendation": recommendation, "details": "{}",
        })
        findings += 1

    # ---- pg_stat_database: cache hit ratio, rollback ratio, temp spill ----
    try:
        resp = rds_data_client.execute_statement(
            resourceArn=target_cluster_arn, secretArn=target_secret_arn, database=database,
            sql=f"/* source=dbops-etl */ {DB_STATS_SQL}", includeResultMetadata=True,
        )
        rows = resp.get("records", [])
        if rows:
            r = rows[0]
            cache_hit = _double(r[0])
            rollback = _double(r[1])
            temp_bytes = _double(r[2])
            metric("pg_cache_hit_ratio", cache_hit)
            metric("pg_rollback_ratio", rollback)
            metric("pg_temp_bytes", temp_bytes)
            # blks_hit+blks_read==0 → cache_hit is NULL→0.0; only flag when there
            # has been real read activity (ratio > 0) so an idle cluster is quiet.
            if 0 < cache_hit < 90:
                finding("pg_cache_hit_low", "warning", "shared-buffers 캐시 히트율",
                        f"{cache_hit:.1f}%", "≥ 90%",
                        "shared_buffers 대비 작업셋이 커 디스크에서 읽고 있습니다. work_mem/shared_buffers "
                        "또는 인스턴스 메모리를 검토하세요.")
            if rollback > 5:
                finding("pg_rollback_high", "warning", "트랜잭션 롤백 비율",
                        f"{rollback:.1f}%", "≤ 5%",
                        "롤백 비율이 높습니다. 애플리케이션 오류/데드락/제약 위반을 확인하세요.")
    except Exception as e:
        errors.append(f"pg_stat_database: {e}")

    # ---- pg_stat_bgwriter: forced-checkpoint ratio ----
    try:
        resp = rds_data_client.execute_statement(
            resourceArn=target_cluster_arn, secretArn=target_secret_arn, database=database,
            sql=f"/* source=dbops-etl */ {BGWRITER_SQL}", includeResultMetadata=True,
        )
        rows = resp.get("records", [])
        if rows:
            forced = _double(rows[0][0])
            metric("pg_checkpoint_forced_ratio", forced)
            if forced > 30:
                finding("pg_forced_checkpoints_high", "warning", "강제 체크포인트 비율",
                        f"{forced:.1f}%", "≤ 30%",
                        "강제(req) 체크포인트가 잦습니다 — WAL이 max_wal_size에 자주 도달한다는 신호입니다. "
                        "max_wal_size 상향을 검토하세요.")
    except Exception as e:
        # PG17 renamed this to pg_stat_checkpointer — skip, never raise.
        errors.append(f"pg_stat_bgwriter: {e}")

    return {"cluster_id": cluster_id, "metrics_inserted": inserted,
            "findings": findings, "errors": errors}
