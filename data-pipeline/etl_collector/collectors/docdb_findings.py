"""DocumentDB Findings Collector — connection saturation, replica lag, cursor timeout, cache hit.

캐시 DB(metric_snapshots)만 읽고 cluster_health_findings에 finding을 적재한다.
라이브 AWS 호출 없음.

Rules:
  - docdb_connection_saturation: peak db_connections / latest db_connections_limit
      ≥ 0.80 → warning, ≥ 0.95 → critical. Skip when limit missing or 0.
  - docdb_replica_lag: peak replica_lag_ms ≥ 1000 → warning, ≥ 10000 → critical.
      Single-instance cluster reports ~0 → silent.
  - docdb_cursor_timeout: SUM(cursors_timed_out) > 0 → warning.
      Indicates unclosed cursors or slow queries holding cursors.
  - docdb_low_cache_hit: AVG(buffer_cache_hit) < 95% with COUNT ≥ 20 samples → warning.
      Working set exceeds instance memory.

All metric_types stored in metric_snapshots use dimensions = '{}' (cluster/table level).
"""

import json
from datetime import datetime, timezone

# Connection saturation thresholds
CONN_SAT_WARNING = 0.80
CONN_SAT_CRITICAL = 0.95

# Replica lag thresholds (milliseconds)
REPLICA_LAG_WARNING_MS = 1000.0
REPLICA_LAG_CRITICAL_MS = 10000.0

# Cache hit ratio threshold (percentage)
CACHE_HIT_WARNING_PCT = 95.0

# Minimum samples for cache hit rule (avoids flagging brand-new/idle clusters)
MIN_CACHE_HIT_SAMPLES = 20

# Cost rightsizing (mirrors Aurora cost_check): 7-day CPU rightsizing on a
# SIZED instance (not burstable db.t* / not serverless). instance_class comes
# from cluster_meta.resource_details (captured by docdb_cw_collector).
COST_CPU_AVG_THRESHOLD = 30.0
COST_CPU_P95_THRESHOLD = 60.0
COST_MIN_CPU_SAMPLES = 20


def _execute(rds_data, cluster_arn, secret_arn, db_name, sql, params=None):
    sql_params = []
    if params:
        for k, v in params.items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn, secretArn=secret_arn, database=db_name,
        sql=f"/* source=dbops-docdbfind */ {sql}", parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for idx, field in enumerate(rec):
            col = cols[idx] if idx < len(cols) and cols[idx] else f"col_{idx}"
            if field.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in field:
                    row[col] = field[typ]
                    break
        out.append(row)
    return out


def collect_docdb_findings(
    rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
    cluster_id, snapshot_ts=None, window_hours=1,
):
    """DocumentDB 클러스터 진단 finding을 cluster_health_findings에 적재.

    snapshot_ts: handler가 넘기는 공유 타임스탬프. 같은 ETL 사이클의 다른
    finding과 동일한 snapshot_time을 공유해야 대시보드 MAX(snapshot_time)
    쿼리에 함께 잡힌다.
    """
    ts = snapshot_ts or datetime.now(timezone.utc).isoformat()

    # --- 단일 집계 쿼리로 모든 필요 지표를 한 번에 읽기 ---
    # db_connections_limit: LAST_VALUE (가장 최근 값)
    # replica_lag_ms / db_connections: MAX over window
    # cursors_timed_out: SUM over window
    # buffer_cache_hit: AVG + COUNT over window
    agg_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  MAX(CASE WHEN metric_type = 'db_connections' THEN value END) AS peak_db_connections, "
        "  (SELECT value FROM metric_snapshots ms2 "
        "   WHERE ms2.cluster_id = :cid AND ms2.metric_type = 'db_connections_limit' "
        "     AND (ms2.dimensions IS NULL OR ms2.dimensions::text = '{}') "
        "   ORDER BY ms2.ts DESC LIMIT 1) AS latest_db_connections_limit, "
        "  MAX(CASE WHEN metric_type = 'replica_lag_ms' THEN value END) AS peak_replica_lag_ms, "
        "  SUM(CASE WHEN metric_type = 'cursors_timed_out' THEN value ELSE 0 END) AS sum_cursors_timed_out, "
        "  AVG(CASE WHEN metric_type = 'buffer_cache_hit' THEN value END) AS avg_buffer_cache_hit, "
        "  COUNT(CASE WHEN metric_type = 'buffer_cache_hit' THEN 1 END) AS cache_hit_samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type IN ('db_connections', 'replica_lag_ms', 'cursors_timed_out', 'buffer_cache_hit') "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id, "hours": str(window_hours)},
    )

    if not agg_rows:
        return {
            "cluster_id": cluster_id,
            "findings_emitted": 0,
        }

    row = agg_rows[0]
    peak_db_connections = row.get("peak_db_connections")
    latest_db_connections_limit = row.get("latest_db_connections_limit")
    peak_replica_lag_ms = row.get("peak_replica_lag_ms")
    sum_cursors_timed_out = float(row.get("sum_cursors_timed_out") or 0.0)
    avg_buffer_cache_hit = row.get("avg_buffer_cache_hit")
    cache_hit_samples = int(row.get("cache_hit_samples") or 0)

    findings = []

    def add(check_type, severity, subject, value_str, threshold_str, recommendation, details):
        findings.append({
            "check_type": check_type,
            "severity": severity,
            "subject": subject,
            "value_str": value_str,
            "threshold_str": threshold_str,
            "recommendation": recommendation,
            "details": json.dumps(details),
        })

    # === 규칙 1: docdb_connection_saturation ===
    # Skip when limit is missing or 0 (avoid div-by-zero and false positives)
    if (
        peak_db_connections is not None
        and latest_db_connections_limit is not None
        and float(latest_db_connections_limit) > 0
    ):
        peak_conn = float(peak_db_connections)
        conn_limit = float(latest_db_connections_limit)
        saturation = peak_conn / conn_limit
        if saturation >= CONN_SAT_WARNING:
            severity = "critical" if saturation >= CONN_SAT_CRITICAL else "warning"
            pct = round(saturation * 100, 1)
            add(
                "docdb_connection_saturation", severity,
                "DocumentDB Connection Saturation",
                f"peak {int(peak_conn)} / limit {int(conn_limit)} ({pct}%)",
                (
                    f"saturation ≥ {int(CONN_SAT_CRITICAL * 100)}%"
                    if severity == "critical"
                    else f"saturation ≥ {int(CONN_SAT_WARNING * 100)}%"
                ),
                (
                    f"최근 {window_hours}시간 peak connection 수가 인스턴스 한도의 {pct}%에 달했습니다 "
                    f"(peak {int(peak_conn)} / limit {int(conn_limit)}). "
                    "connection pooling 도입 / 인스턴스 클래스 상향 / 연결 누수 점검을 권장합니다."
                ),
                {
                    "peak_db_connections": peak_conn,
                    "db_connections_limit": conn_limit,
                    "saturation": round(saturation, 4),
                    "window_hours": window_hours,
                },
            )

    # === 규칙 2: docdb_replica_lag ===
    if peak_replica_lag_ms is not None:
        lag_ms = float(peak_replica_lag_ms)
        if lag_ms >= REPLICA_LAG_WARNING_MS:
            severity = "critical" if lag_ms >= REPLICA_LAG_CRITICAL_MS else "warning"
            add(
                "docdb_replica_lag", severity,
                "DocumentDB Replica Lag",
                f"peak {lag_ms:.0f} ms",
                (
                    f"replica_lag ≥ {int(REPLICA_LAG_CRITICAL_MS)} ms"
                    if severity == "critical"
                    else f"replica_lag ≥ {int(REPLICA_LAG_WARNING_MS)} ms"
                ),
                (
                    f"최근 {window_hours}시간 replica lag peak이 {lag_ms:.0f} ms입니다. "
                    "쓰기 부하 완화 / 리더 인스턴스 확장 / 장기 실행 op 점검을 권장합니다."
                ),
                {
                    "peak_replica_lag_ms": lag_ms,
                    "window_hours": window_hours,
                },
            )

    # === 규칙 3: docdb_cursor_timeout ===
    if sum_cursors_timed_out > 0:
        add(
            "docdb_cursor_timeout", "warning",
            "DocumentDB Cursor Timeout",
            f"cursor timeout {int(sum_cursors_timed_out)}건",
            "cursors_timed_out > 0",
            (
                f"최근 {window_hours}시간 동안 cursor timeout이 {int(sum_cursors_timed_out)}건 발생했습니다. "
                "앱이 cursor를 닫지 않거나 느린 쿼리가 cursor를 점유하고 있습니다 — "
                "쿼리 패턴/cursor 수명 점검을 권장합니다."
            ),
            {
                "sum_cursors_timed_out": int(sum_cursors_timed_out),
                "window_hours": window_hours,
            },
        )

    # === 규칙 4: docdb_low_cache_hit ===
    # Require ≥ MIN_CACHE_HIT_SAMPLES to avoid flagging brand-new idle clusters
    if (
        avg_buffer_cache_hit is not None
        and cache_hit_samples >= MIN_CACHE_HIT_SAMPLES
        and float(avg_buffer_cache_hit) < CACHE_HIT_WARNING_PCT
    ):
        avg_hit = float(avg_buffer_cache_hit)
        add(
            "docdb_low_cache_hit", "warning",
            "DocumentDB Low Buffer Cache Hit",
            f"avg cache hit {avg_hit:.1f}% ({cache_hit_samples} 샘플)",
            f"avg buffer_cache_hit < {CACHE_HIT_WARNING_PCT:.0f}% (샘플 ≥ {MIN_CACHE_HIT_SAMPLES}개)",
            (
                f"최근 {window_hours}시간 평균 buffer cache hit ratio가 {avg_hit:.1f}%로 낮습니다. "
                "워킹셋이 인스턴스 메모리를 초과하고 있습니다 — 인스턴스 클래스 상향을 권장합니다."
            ),
            {
                "avg_buffer_cache_hit": round(avg_hit, 2),
                "cache_hit_samples": cache_hit_samples,
                "threshold_pct": CACHE_HIT_WARNING_PCT,
                "window_hours": window_hours,
            },
        )

    # === 규칙 5: docdb_cost_oversized (7일 CPU rightsizing) ===
    # Aurora cost_check의 DocDB판. instance_class는 collector가 resource_details에
    # 담은 writer 클래스. 버스터블(db.t*)/서버리스/미수집은 skip(축소 권고 부적합).
    # 이 규칙의 SELECT 실패가 위 4개 운영 finding의 INSERT를 막지 않도록 try로 격리.
    try:
        meta_rows = _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "SELECT resource_details->>'instance_class' AS instance_class "
            "FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
        instance_class = (meta_rows[0].get("instance_class") if meta_rows else None) or ""
        ic_lower = instance_class.lower()
        if instance_class and "serverless" not in ic_lower and not ic_lower.startswith("db.t"):
            cpu_rows = _execute(
                rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
                "SELECT AVG(value) AS avg_cpu, "
                "  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95_cpu, "
                "  COUNT(*) AS samples "
                "FROM metric_snapshots "
                "WHERE cluster_id = :cid AND metric_type = 'cpu_utilization' "
                "  AND ts > NOW() - INTERVAL '7 days' "
                "  AND (dimensions IS NULL OR dimensions::text = '{}')",
                {"cid": cluster_id},
            )
            row2 = cpu_rows[0] if cpu_rows else {}
            samples = int(row2.get("samples") or 0)
            if samples >= COST_MIN_CPU_SAMPLES and row2.get("avg_cpu") is not None:
                avg_cpu = float(row2.get("avg_cpu") or 0)
                p95_cpu = float(row2.get("p95_cpu") or 0)
                if avg_cpu < COST_CPU_AVG_THRESHOLD and p95_cpu < COST_CPU_P95_THRESHOLD:
                    add(
                        "docdb_cost_oversized", "info", instance_class,
                        f"7일 평균 CPU {avg_cpu:.1f}% / p95 {p95_cpu:.1f}% ({samples} 샘플)",
                        f"avg < {COST_CPU_AVG_THRESHOLD:.0f}% & p95 < {COST_CPU_P95_THRESHOLD:.0f}% → 한 단계 축소 검토",
                        (
                            f"{instance_class}의 7일 평균 CPU가 {avg_cpu:.1f}%입니다 — 한 단계 작은 "
                            "인스턴스 클래스를 검토하세요 (보통 월 30-50% 절감). 축소 후 프로덕션 "
                            "트래픽 1주를 지켜보고 재평가하세요."
                        ),
                        {
                            "instance_class": instance_class,
                            "avg_cpu": round(avg_cpu, 2),
                            "p95_cpu": round(p95_cpu, 2),
                            "samples": samples,
                            "window_days": 7,
                        },
                    )
    except Exception as e:
        print(f"[docdb_findings] cost rule failed for {cluster_id}: {e}")

    # --- 단일 snapshot_time으로 일괄 적재 ---
    for finding in findings:
        _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "INSERT INTO cluster_health_findings "
            "(cluster_id, snapshot_time, check_type, severity, subject, "
            "value_str, threshold_str, recommendation, details) "
            "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, "
            ":value_str, :threshold_str, :recommendation, :details::jsonb)",
            {
                "cluster_id": cluster_id,
                "ts": ts,
                "check_type": finding["check_type"],
                "severity": finding["severity"],
                "subject": finding["subject"],
                "value_str": finding["value_str"],
                "threshold_str": finding["threshold_str"],
                "recommendation": finding["recommendation"],
                "details": finding["details"],
            },
        )

    return {
        "cluster_id": cluster_id,
        "findings_emitted": len(findings),
        "peak_db_connections": float(peak_db_connections) if peak_db_connections is not None else None,
        "latest_db_connections_limit": float(latest_db_connections_limit) if latest_db_connections_limit is not None else None,
        "peak_replica_lag_ms": float(peak_replica_lag_ms) if peak_replica_lag_ms is not None else None,
        "sum_cursors_timed_out": int(sum_cursors_timed_out),
        "avg_buffer_cache_hit": float(avg_buffer_cache_hit) if avg_buffer_cache_hit is not None else None,
    }
