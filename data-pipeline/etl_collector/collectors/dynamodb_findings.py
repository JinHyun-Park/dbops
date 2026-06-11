"""DynamoDB Findings Collector — throttling, capacity fit, hot-partition 진단.

캐시 DB(cluster_meta.resource_details + metric_snapshots)만 읽고
cluster_health_findings에 finding을 적재한다. 라이브 AWS 호출 없음.

capacity unit math:
  ConsumedReadCapacityUnits  = 1분 Sum (해당 분에 소비된 RCU 총합)
  ProvisionedReadCapacityUnits = per-second Average (초당 프로비저닝 RCU)
  → 1분 utilization = consumed_in_minute / (60 * provisioned_per_second)

Fix 2: per-minute utilization은 각 consumed 데이터포인트마다 해당 ts 직전의
provisioned 값을 LATERAL JOIN으로 조회해 나눈다. MAX(consumed)/MAX(provisioned)
독립 집계 방식은 프로비저닝 변경 시 오류를 유발하므로 사용하지 않는다.

Fix 1: throttle은 read / write 두 side로 분리 집계한다.
  - read_throttle  = SUM of read_throttle_events
  - write_throttle = SUM of write_throttle_events + throttled_requests
    (throttled_requests는 일반 write-ish 제한으로 write side에 합산)
  총합(throttle_total)은 ddb_throttling 카운트 메시지에 사용한다.

Fix 3: util > 100% 시 value_str/recommendation에 burst 설명을 추가한다.
  WCU/RCU/burst 등 전문 용어는 영어 유지.

Fix 4: ddb_capacity_underprovisioned는 단일 분 peak이 아닌 sustained 고부하
(high_minutes ≥ 3 — ≥80% 유틸이 3분 이상)일 때만 발생한다.
"""

import json
from datetime import datetime, timezone

# on-demand 고처리량 판정 임계 — 1분 Sum ≥ 6000 ≈ 100 RCU/s 지속
ONDEMAND_HIGH_THRESHOLD = 6000.0

# 과다 프로비저닝 판정: peak util(r AND w) ≤ 이 값 + 충분한 표본
OVER_UTIL_THRESHOLD = 0.20

# 부족 프로비저닝 판정: ≥80% 유틸 분 수 기준
UNDER_UTIL_THRESHOLD = 0.80

# 부족 프로비저닝 sustained 판정: ≥80% 유틸이 이 분 수 이상
UNDER_SUSTAINED_MINUTES = 3

# 핫 파티션 판정: 해당 side throttle > 0 AND 해당 side peak util < 이 값
HOT_PARTITION_UTIL_CAP = 0.50

# 과다 프로비저닝 판정에 필요한 최소 consumed 데이터포인트 수
MIN_CONSUMED_DATAPOINTS = 20

# throttle critical 기준
THROTTLE_CRITICAL_TOTAL = 100
THROTTLE_CRITICAL_MINUTES = 10

# burst 초과 설명 (Fix 3)
_BURST_EXPLANATION = " (>100% = burst capacity로 프로비저닝 한도를 일시 초과)"


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


def _burst_suffix(util_value):
    """Return burst explanation suffix if util > 100%, else empty string."""
    if util_value is not None and util_value > 1.0:
        return _BURST_EXPLANATION
    return ""


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

    # --- 2) throttle aggregates — per-side split (Fix 1) ---
    # read_throttle  = SUM(read_throttle_events)
    # write_throttle = SUM(write_throttle_events + throttled_requests)
    #   throttled_requests is a general/write-ish metric → grouped with write side
    throttle_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  COALESCE(SUM(CASE WHEN metric_type = 'read_throttle_events' THEN value ELSE 0 END), 0) AS read_throttle, "
        "  COALESCE(SUM(CASE WHEN metric_type IN ('write_throttle_events', 'throttled_requests') THEN value ELSE 0 END), 0) AS write_throttle, "
        "  COALESCE(SUM(value), 0) AS throttle_total, "
        "  COUNT(DISTINCT CASE WHEN value > 0 THEN date_trunc('minute', ts) END) AS throttle_minutes "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type IN ('read_throttle_events', 'write_throttle_events', 'throttled_requests') "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id, "hours": str(window_hours)},
    )
    read_throttle = 0.0
    write_throttle = 0.0
    throttle_total = 0.0
    throttle_minutes = 0
    if throttle_rows:
        row = throttle_rows[0]
        read_throttle = float(row.get("read_throttle") or 0)
        write_throttle = float(row.get("write_throttle") or 0)
        throttle_total = float(row.get("throttle_total") or 0)
        throttle_minutes = int(row.get("throttle_minutes") or 0)

    # --- 3) per-minute timestamp-aligned utilization via LATERAL JOIN (Fix 2) ---
    # For each consumed datapoint, divide by the provisioned value effective at that
    # timestamp (closest provisioned row with ts ≤ consumed ts, per-cluster).
    # This gives accurate utilization even across provisioning changes.
    # If no provisioned datapoints exist (PAY_PER_REQUEST), the lateral join yields
    # zero rows → peak_util stays 0/None and provisioned rules stay silent.
    # NULLIF guards divide-by-zero.
    # high_minutes_* = count of minutes with util ≥ 80% (used by Fix 4).
    util_rcu_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  COALESCE(MAX(util), 0) AS peak_util_r, "
        "  COUNT(*) FILTER (WHERE util >= 0.8) AS high_minutes_r, "
        "  COUNT(*) AS n_r "
        "FROM ( "
        "  SELECT c.ts, c.value / (60.0 * NULLIF(p.prov, 0)) AS util "
        "  FROM metric_snapshots c "
        "  CROSS JOIN LATERAL ( "
        "    SELECT value AS prov FROM metric_snapshots pp "
        "    WHERE pp.cluster_id = c.cluster_id AND pp.metric_type = 'provisioned_rcu' "
        "      AND pp.ts <= c.ts AND (pp.dimensions IS NULL OR pp.dimensions::text = '{}') "
        "    ORDER BY pp.ts DESC LIMIT 1 "
        "  ) p "
        "  WHERE c.cluster_id = :cid AND c.metric_type = 'consumed_rcu' "
        "    AND c.ts > NOW() - (:hours || ' hours')::interval "
        "    AND (c.dimensions IS NULL OR c.dimensions::text = '{}') "
        ") x",
        {"cid": cluster_id, "hours": str(window_hours)},
    )
    util_wcu_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  COALESCE(MAX(util), 0) AS peak_util_w, "
        "  COUNT(*) FILTER (WHERE util >= 0.8) AS high_minutes_w, "
        "  COUNT(*) AS n_w "
        "FROM ( "
        "  SELECT c.ts, c.value / (60.0 * NULLIF(p.prov, 0)) AS util "
        "  FROM metric_snapshots c "
        "  CROSS JOIN LATERAL ( "
        "    SELECT value AS prov FROM metric_snapshots pp "
        "    WHERE pp.cluster_id = c.cluster_id AND pp.metric_type = 'provisioned_wcu' "
        "      AND pp.ts <= c.ts AND (pp.dimensions IS NULL OR pp.dimensions::text = '{}') "
        "    ORDER BY pp.ts DESC LIMIT 1 "
        "  ) p "
        "  WHERE c.cluster_id = :cid AND c.metric_type = 'consumed_wcu' "
        "    AND c.ts > NOW() - (:hours || ' hours')::interval "
        "    AND (c.dimensions IS NULL OR c.dimensions::text = '{}') "
        ") x",
        {"cid": cluster_id, "hours": str(window_hours)},
    )

    # Read-side util results
    # n_r == 0 means no lateral-join rows (no provisioned data) → util is unknown
    peak_util_r = None
    high_minutes_r = 0
    n_r = 0
    if util_rcu_rows:
        rrow = util_rcu_rows[0]
        n_r = int(rrow.get("n_r") or 0)
        if n_r > 0:
            raw_r = rrow.get("peak_util_r")
            peak_util_r = float(raw_r) if raw_r is not None else None
            high_minutes_r = int(rrow.get("high_minutes_r") or 0)

    # Write-side util results
    peak_util_w = None
    high_minutes_w = 0
    n_w = 0
    if util_wcu_rows:
        wrow = util_wcu_rows[0]
        n_w = int(wrow.get("n_w") or 0)
        if n_w > 0:
            raw_w = wrow.get("peak_util_w")
            peak_util_w = float(raw_w) if raw_w is not None else None
            high_minutes_w = int(wrow.get("high_minutes_w") or 0)

    # consumed_datapoints for overprovisioned sample-floor check
    consumed_datapoints = n_r  # RCU side is representative

    # On-demand peak consumed (raw units/min, not divided by provisioned)
    # Still needed for ddb_ondemand_high_throughput rule.
    # Derive from the lateral query n_r==0 case: we need raw MAX consumed too.
    # Use a lightweight aggregate query for raw consumed max (both sides).
    raw_consumed_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  MAX(CASE WHEN metric_type = 'consumed_rcu' THEN value END) AS max_consumed_rcu, "
        "  MAX(CASE WHEN metric_type = 'consumed_wcu' THEN value END) AS max_consumed_wcu "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type IN ('consumed_rcu', 'consumed_wcu') "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id, "hours": str(window_hours)},
    )
    max_consumed_rcu = 0.0
    max_consumed_wcu = 0.0
    if raw_consumed_rows:
        rcrow = raw_consumed_rows[0]
        max_consumed_rcu = float(rcrow.get("max_consumed_rcu") or 0)
        max_consumed_wcu = float(rcrow.get("max_consumed_wcu") or 0)

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
        # per-side breakdown in message when both sides have data
        side_detail = f"(read {int(read_throttle)}건 / write {int(write_throttle)}건)"
        add(
            "ddb_throttling", severity, "DynamoDB Throttle",
            f"throttle {int(throttle_total)}건 / {throttle_minutes}분",
            (
                f"throttle ≥ {THROTTLE_CRITICAL_TOTAL}건 또는 ≥ {THROTTLE_CRITICAL_MINUTES}분"
                if is_critical else "throttle > 0"
            ),
            (
                f"최근 {window_hours}시간 동안 {int(throttle_total)}건의 throttle이 "
                f"{throttle_minutes}분에 걸쳐 발생했습니다 {side_detail}. "
                "처리 가능 용량(RCU/WCU)을 높이거나, on-demand 모드로 전환하거나, "
                "hot partition key 분산을 검토하세요."
            ),
            {
                "throttle_total": int(throttle_total),
                "read_throttle": int(read_throttle),
                "write_throttle": int(write_throttle),
                "throttle_minutes": throttle_minutes,
                "window_hours": window_hours,
                "billing_mode": billing_mode,
            },
        )

    # === 규칙 2: ddb_capacity_underprovisioned (PROVISIONED only) ===
    # Fix 4: require SUSTAINED high utilization (≥3 minutes at ≥80%) rather than
    # a single-minute peak spike. high_minutes_r/w come from the lateral-join query.
    sustained_under = (
        (high_minutes_r >= UNDER_SUSTAINED_MINUTES)
        or (high_minutes_w >= UNDER_SUSTAINED_MINUTES)
    )
    if is_provisioned and sustained_under:
        util_r_pct = round((peak_util_r or 0.0) * 100, 1)
        util_w_pct = round((peak_util_w or 0.0) * 100, 1)
        # Fix 3: append burst explanation if either side exceeds 100%
        burst_r = _burst_suffix(peak_util_r)
        burst_w = _burst_suffix(peak_util_w)
        burst_note = burst_r or burst_w  # first non-empty, or ""
        add(
            "ddb_capacity_underprovisioned", "warning",
            "DynamoDB Capacity (Underprovisioned)",
            f"peak util R={util_r_pct}% / W={util_w_pct}%{burst_note}",
            (
                f"≥{UNDER_UTIL_THRESHOLD*100:.0f}% util이 "
                f"≥{UNDER_SUSTAINED_MINUTES}분 지속 (R={high_minutes_r}분 / W={high_minutes_w}분)"
            ),
            (
                f"프로비저닝 용량 대비 peak RCU 사용률 {util_r_pct}%"
                f"{burst_r}, WCU 사용률 {util_w_pct}%{burst_w}입니다. "
                f"≥{UNDER_UTIL_THRESHOLD*100:.0f}% 구간이 "
                f"R {high_minutes_r}분 / W {high_minutes_w}분 지속되었습니다. "
                "RCU/WCU를 높이거나 auto-scaling을 활성화하세요."
            ),
            {
                "peak_util_r": round(peak_util_r or 0.0, 4),
                "peak_util_w": round(peak_util_w or 0.0, 4),
                "high_minutes_r": high_minutes_r,
                "high_minutes_w": high_minutes_w,
                "window_hours": window_hours,
            },
        )

    # === 규칙 3: ddb_capacity_overprovisioned (PROVISIONED only) ===
    # both r AND w ≤ 20%, enough samples, prov > 0 for both (n_r > 0 AND n_w > 0)
    # Note: a longer observation window (e.g. 24h) would be preferable for this rule
    # to avoid false positives during low-traffic windows, but behavior is unchanged here.
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
                "window_hours": window_hours,
            },
        )

    # === 규칙 4: ddb_hot_partition (PROVISIONED only) ===
    # Fix 1: correlate throttled SIDE with THAT side's headroom.
    # - read_throttle > 0 AND peak_util_r < 50% (read side has headroom → read hot partition)
    # - write_throttle > 0 AND peak_util_w < 50% (write side has headroom → write hot partition)
    # If the throttled side's util is unknown (None, no provisioned data), do NOT fire.
    hot_read = (
        read_throttle > 0
        and peak_util_r is not None
        and peak_util_r < HOT_PARTITION_UTIL_CAP
    )
    hot_write = (
        write_throttle > 0
        and peak_util_w is not None
        and peak_util_w < HOT_PARTITION_UTIL_CAP
    )
    if is_provisioned and (hot_read or hot_write):
        # Compose a combined message covering whichever side(s) triggered
        sides = []
        if hot_read:
            sides.append(f"R={round(peak_util_r * 100, 1)}% (read throttle {int(read_throttle)}건)")
        if hot_write:
            sides.append(f"W={round(peak_util_w * 100, 1)}% (write throttle {int(write_throttle)}건)")
        side_str = " / ".join(sides)
        add(
            "ddb_hot_partition", "warning",
            "DynamoDB Hot Partition",
            f"util {side_str} 인데 throttle 발생",
            f"side throttle > 0 AND 해당 side peak util < {HOT_PARTITION_UTIL_CAP*100:.0f}%",
            (
                f"프로비저닝 헤드룸이 있음에도 throttle이 발생했습니다({side_str}) — "
                "partition key 편중으로 특정 파티션만 throttle 되는 전형적 패턴입니다. "
                "partition key 설계를 재검토하거나 write sharding을 고려하세요."
            ),
            {
                "read_throttle": int(read_throttle),
                "write_throttle": int(write_throttle),
                "peak_util_r": round(peak_util_r or 0.0, 4),
                "peak_util_w": round(peak_util_w or 0.0, 4),
                "hot_read": hot_read,
                "hot_write": hot_write,
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
        "read_throttle": int(read_throttle),
        "write_throttle": int(write_throttle),
        "peak_util_r": round(peak_util_r, 4) if peak_util_r is not None else None,
        "peak_util_w": round(peak_util_w, 4) if peak_util_w is not None else None,
        "high_minutes_r": high_minutes_r,
        "high_minutes_w": high_minutes_w,
    }
