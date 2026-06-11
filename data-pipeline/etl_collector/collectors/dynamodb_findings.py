"""DynamoDB Findings Collector — throttling, capacity fit, hot-partition 진단.

캐시 DB(cluster_meta.resource_details + metric_snapshots)만 읽고
cluster_health_findings에 finding을 적재한다. 라이브 AWS 호출 없음.

capacity unit math:
  ConsumedReadCapacityUnits  = 1분 Sum (해당 분에 소비된 RCU 총합)
  ProvisionedReadCapacityUnits = per-second Average (초당 프로비저닝 RCU)
  → 1분 utilization = consumed_in_minute / (60 * provisioned_per_second)
"""

import json
from datetime import datetime, timezone

# on-demand 고처리량 판정 임계 — 1분 Sum ≥ 6000 ≈ 100 RCU/s 지속
ONDEMAND_HIGH_THRESHOLD = 6000.0

# 과다 프로비저닝 판정: peak util(r AND w) ≤ 이 값 + 충분한 표본
OVER_UTIL_THRESHOLD = 0.20

# 부족 프로비저닝 판정: peak util(r OR w) ≥ 이 값
UNDER_UTIL_THRESHOLD = 0.80

# 핫 파티션 판정: throttle > 0 AND peak util < 이 값 (헤드룸이 있는데 throttle)
HOT_PARTITION_UTIL_CAP = 0.50

# 과다 프로비저닝 판정에 필요한 최소 consumed 데이터포인트 수
MIN_CONSUMED_DATAPOINTS = 20

# throttle critical 기준
THROTTLE_CRITICAL_TOTAL = 100
THROTTLE_CRITICAL_MINUTES = 10


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
        sql=f"/* source=dbops-ddbfind */ {sql}", parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for i, field in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if field.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in field:
                    row[col] = field[typ]
                    break
        out.append(row)
    return out


def collect_dynamodb_findings(
    rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
    cluster_id, snapshot_ts=None, window_hours=1,
):
    """DynamoDB 클러스터 진단 finding을 cluster_health_findings에 적재.

    snapshot_ts: handler가 넘기는 공유 타임스탬프. 같은 ETL 사이클의 다른
    finding과 동일한 snapshot_time을 공유해야 대시보드 MAX(snapshot_time)
    쿼리에 함께 잡힌다.
    """
    ts = snapshot_ts or datetime.now(timezone.utc).isoformat()

    # --- 1) billing_mode: cluster_meta.resource_details (JSONB) ---
    meta_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT resource_details FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    billing_mode = None
    if meta_rows:
        rd = meta_rows[0].get("resource_details")
        if isinstance(rd, str):
            try:
                rd = json.loads(rd)
            except (json.JSONDecodeError, TypeError):
                rd = {}
        if isinstance(rd, dict):
            billing_mode = rd.get("billing_mode")

    # --- 2) throttle aggregates ---
    throttle_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  COALESCE(SUM(value), 0) AS throttle_total, "
        "  COUNT(DISTINCT CASE WHEN value > 0 THEN date_trunc('minute', ts) END) AS throttle_minutes "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type IN ('read_throttle_events', 'write_throttle_events', 'throttled_requests') "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id, "hours": str(window_hours)},
    )
    throttle_total = 0.0
    throttle_minutes = 0
    if throttle_rows:
        throttle_total = float(throttle_rows[0].get("throttle_total") or 0)
        throttle_minutes = int(throttle_rows[0].get("throttle_minutes") or 0)

    # --- 3) consumed + provisioned aggregates ---
    capacity_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  MAX(CASE WHEN metric_type = 'consumed_rcu' THEN value END) AS max_consumed_rcu, "
        "  MAX(CASE WHEN metric_type = 'consumed_wcu' THEN value END) AS max_consumed_wcu, "
        "  MAX(CASE WHEN metric_type = 'provisioned_rcu' THEN value END) AS prov_rcu, "
        "  MAX(CASE WHEN metric_type = 'provisioned_wcu' THEN value END) AS prov_wcu, "
        "  COUNT(CASE WHEN metric_type = 'consumed_rcu' THEN 1 END) AS consumed_datapoints "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type IN ('consumed_rcu', 'consumed_wcu', 'provisioned_rcu', 'provisioned_wcu') "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id, "hours": str(window_hours)},
    )
    max_consumed_rcu = 0.0
    max_consumed_wcu = 0.0
    prov_rcu = None
    prov_wcu = None
    consumed_datapoints = 0
    if capacity_rows:
        row = capacity_rows[0]
        max_consumed_rcu = float(row.get("max_consumed_rcu") or 0)
        max_consumed_wcu = float(row.get("max_consumed_wcu") or 0)
        raw_prov_rcu = row.get("prov_rcu")
        raw_prov_wcu = row.get("prov_wcu")
        prov_rcu = float(raw_prov_rcu) if raw_prov_rcu is not None else None
        prov_wcu = float(raw_prov_wcu) if raw_prov_wcu is not None else None
        consumed_datapoints = int(row.get("consumed_datapoints") or 0)

    # --- peak utilization (guard divide-by-zero) ---
    peak_util_r = None
    peak_util_w = None
    if prov_rcu and prov_rcu > 0:
        peak_util_r = max_consumed_rcu / (60.0 * prov_rcu)
    if prov_wcu and prov_wcu > 0:
        peak_util_w = max_consumed_wcu / (60.0 * prov_wcu)

    # highest of the two sides (whichever is available)
    peak_util_max = None
    if peak_util_r is not None and peak_util_w is not None:
        peak_util_max = max(peak_util_r, peak_util_w)
    elif peak_util_r is not None:
        peak_util_max = peak_util_r
    elif peak_util_w is not None:
        peak_util_max = peak_util_w

    is_provisioned = (billing_mode == "PROVISIONED")
    is_ondemand = (billing_mode == "PAY_PER_REQUEST")

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

    # === 규칙 1: ddb_throttling ===
    if throttle_total > 0:
        is_critical = (
            throttle_total >= THROTTLE_CRITICAL_TOTAL
            or throttle_minutes >= THROTTLE_CRITICAL_MINUTES
        )
        severity = "critical" if is_critical else "warning"
        add(
            "ddb_throttling", severity, "DynamoDB Throttle",
            f"throttle {int(throttle_total)}건 / {throttle_minutes}분",
            (
                f"throttle ≥ {THROTTLE_CRITICAL_TOTAL}건 또는 ≥ {THROTTLE_CRITICAL_MINUTES}분"
                if is_critical else "throttle > 0"
            ),
            (
                f"최근 {window_hours}시간 동안 {int(throttle_total)}건의 throttle이 "
                f"{throttle_minutes}분에 걸쳐 발생했습니다. "
                "처리 가능 용량(RCU/WCU)을 높이거나, on-demand 모드로 전환하거나, "
                "hot partition key 분산을 검토하세요."
            ),
            {
                "throttle_total": int(throttle_total),
                "throttle_minutes": throttle_minutes,
                "window_hours": window_hours,
                "billing_mode": billing_mode,
            },
        )

    # === 규칙 2: ddb_capacity_underprovisioned (PROVISIONED only, prov > 0) ===
    if is_provisioned and peak_util_max is not None and peak_util_max >= UNDER_UTIL_THRESHOLD:
        util_r_pct = round((peak_util_r or 0.0) * 100, 1)
        util_w_pct = round((peak_util_w or 0.0) * 100, 1)
        add(
            "ddb_capacity_underprovisioned", "warning",
            "DynamoDB Capacity (Underprovisioned)",
            f"peak util R={util_r_pct}% / W={util_w_pct}%",
            f"peak util ≥ {UNDER_UTIL_THRESHOLD*100:.0f}%",
            (
                f"프로비저닝 용량 대비 peak RCU 사용률 {util_r_pct}%, "
                f"WCU 사용률 {util_w_pct}%입니다. "
                "RCU/WCU를 높이거나 auto-scaling을 활성화하세요."
            ),
            {
                "peak_util_r": round(peak_util_r or 0.0, 4),
                "peak_util_w": round(peak_util_w or 0.0, 4),
                "max_consumed_rcu": max_consumed_rcu,
                "max_consumed_wcu": max_consumed_wcu,
                "prov_rcu": prov_rcu,
                "prov_wcu": prov_wcu,
                "window_hours": window_hours,
            },
        )

    # === 규칙 3: ddb_capacity_overprovisioned (PROVISIONED only) ===
    # both r AND w ≤ 20%, enough samples, prov > 0 for both
    if (
        is_provisioned
        and peak_util_r is not None
        and peak_util_w is not None
        and peak_util_r <= OVER_UTIL_THRESHOLD
        and peak_util_w <= OVER_UTIL_THRESHOLD
        and consumed_datapoints >= MIN_CONSUMED_DATAPOINTS
    ):
        util_r_pct = round(peak_util_r * 100, 1)
        util_w_pct = round(peak_util_w * 100, 1)
        add(
            "ddb_capacity_overprovisioned", "info",
            "DynamoDB Capacity (Overprovisioned)",
            f"peak util R={util_r_pct}% / W={util_w_pct}%",
            f"peak util(r AND w) ≤ {OVER_UTIL_THRESHOLD*100:.0f}% (샘플 {consumed_datapoints}개)",
            (
                f"최근 {window_hours}시간 peak RCU 사용률 {util_r_pct}%, "
                f"WCU 사용률 {util_w_pct}%로 매우 낮습니다. "
                "RCU/WCU를 줄이거나 on-demand 모드로 전환해 비용을 절감하세요."
            ),
            {
                "peak_util_r": round(peak_util_r, 4),
                "peak_util_w": round(peak_util_w, 4),
                "consumed_datapoints": consumed_datapoints,
                "prov_rcu": prov_rcu,
                "prov_wcu": prov_wcu,
                "window_hours": window_hours,
            },
        )

    # === 규칙 4: ddb_hot_partition (PROVISIONED only) ===
    # throttle > 0 AND peak util < 50% → 헤드룸이 있는데 throttle → 파티션 편중 의심
    if (
        is_provisioned
        and throttle_total > 0
        and peak_util_max is not None
        and peak_util_max < HOT_PARTITION_UTIL_CAP
    ):
        util_pct = round(peak_util_max * 100, 1)
        add(
            "ddb_hot_partition", "warning",
            "DynamoDB Hot Partition",
            f"peak util {util_pct}% 인데 throttle {int(throttle_total)}건",
            f"throttle > 0 AND peak util < {HOT_PARTITION_UTIL_CAP*100:.0f}%",
            (
                f"프로비저닝 헤드룸({100 - util_pct:.0f}% 이상 남음)이 있음에도 "
                f"throttle {int(throttle_total)}건이 발생했습니다 — "
                "partition key 편중으로 특정 파티션만 throttle 되는 전형적 패턴입니다. "
                "partition key 설계를 재검토하거나 write sharding을 고려하세요."
            ),
            {
                "throttle_total": int(throttle_total),
                "peak_util_max": round(peak_util_max, 4),
                "peak_util_r": round(peak_util_r or 0.0, 4),
                "peak_util_w": round(peak_util_w or 0.0, 4),
                "window_hours": window_hours,
            },
        )

    # === 규칙 5: ddb_gsi_throttling (per-GSI, any billing mode) ===
    # Query metric_snapshots for rows with a non-NULL "gsi" dimension key,
    # grouped by GSI name, summing read + write throttle events.
    try:
        gsi_throttle_rows = _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "SELECT "
            "  dimensions->>'gsi' AS gsi_name, "
            "  COALESCE(SUM(value), 0) AS gsi_throttle_total "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid "
            "  AND metric_type IN ('read_throttle_events', 'write_throttle_events') "
            "  AND ts > NOW() - (:hours || ' hours')::interval "
            "  AND dimensions->>'gsi' IS NOT NULL "
            "GROUP BY dimensions->>'gsi' "
            "HAVING COALESCE(SUM(value), 0) > 0",
            {"cid": cluster_id, "hours": str(window_hours)},
        )
    except Exception:
        gsi_throttle_rows = []

    for gsi_row in (gsi_throttle_rows or []):
        gsi_nm = gsi_row.get("gsi_name") or ""
        gsi_throttle = float(gsi_row.get("gsi_throttle_total") or 0)
        if not gsi_nm or gsi_throttle <= 0:
            continue
        add(
            "ddb_gsi_throttling", "warning",
            gsi_nm,
            f"GSI {gsi_nm} throttle {int(gsi_throttle)}건",
            "GSI throttle > 0",
            (
                f"GSI {gsi_nm}이(가) under-provisioned이거나 hot — "
                "GSI 용량 상향 또는 GSI 키 재설계 검토"
            ),
            {
                "gsi_name": gsi_nm,
                "gsi_throttle_total": int(gsi_throttle),
                "window_hours": window_hours,
                "billing_mode": billing_mode,
            },
        )

    # === 규칙 6: ddb_ondemand_high_throughput (PAY_PER_REQUEST only) ===
    if is_ondemand and max(max_consumed_rcu, max_consumed_wcu) >= ONDEMAND_HIGH_THRESHOLD:
        peak_units = max(max_consumed_rcu, max_consumed_wcu)
        add(
            "ddb_ondemand_high_throughput", "info",
            "DynamoDB On-Demand High Throughput",
            f"peak consumed {peak_units:.0f} units/min",
            f"consumed ≥ {ONDEMAND_HIGH_THRESHOLD:.0f} units/min",
            (
                f"on-demand 테이블의 peak 처리량이 분당 {peak_units:.0f} RCU/WCU(≈ "
                f"{peak_units/60:.0f}/s)에 달합니다. "
                "이 수준이 지속된다면 provisioned + auto-scaling으로 전환해 비용을 최적화하는 방안을 검토하세요."
            ),
            {
                "max_consumed_rcu": max_consumed_rcu,
                "max_consumed_wcu": max_consumed_wcu,
                "threshold_units_per_min": ONDEMAND_HIGH_THRESHOLD,
                "window_hours": window_hours,
            },
        )

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
        "billing_mode": billing_mode,
        "findings_emitted": len(findings),
        "throttle_total": int(throttle_total),
        "peak_util_r": round(peak_util_r, 4) if peak_util_r is not None else None,
        "peak_util_w": round(peak_util_w, 4) if peak_util_w is not None else None,
    }
