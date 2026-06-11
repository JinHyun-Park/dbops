"""고갈 예측 경보 — storage/connection/ACU가 한계에 도달하는 ETA를 사전 경고.

대시보드의 Capacity Forecast 패널(_capacity_forecast)은 DBA가 직접 메트릭을
골라 봐야 보인다. 이 collector는 그 예측을 매 수집 사이클에 자동으로 돌려,
한계 도달이 임박(기본 14일 이내)하면 finding으로 띄운다 — DBA가 패널을
들여다보지 않아도 "현 추세로 N일 후 connection 한도" 경고가 Maintenance
Health에 능동적으로 올라온다.

엔진 무관(engine-agnostic): storage/connection은 PostgreSQL·MySQL 양쪽 모두
같은 metric_snapshots(VolumeBytesUsed·DatabaseConnections)에서 나오고, ACU는
Serverless v2 클러스터(엔진 불문)면 ServerlessDatabaseCapacity가 잡힌다.
그래서 PG/MySQL 핸들러 양쪽에서 동일하게 호출한다.

선형 추세(REGR_SLOPE)를 실제 한계(connection=cluster_meta.max_connections,
storage=Aurora 볼륨 상한 128 TiB)와 비교해 days_until을 계산한다. 추세가
증가하지 않거나(slope≤0) 표본이 부족하면 침묵한다 — 노이즈로 거짓 경보를
내지 않는다. ETA가 가까울수록 심각도를 높인다(≤3일 critical, ≤7일 warning,
≤14일 info).

ACU는 특수 취급한다. Serverless v2의 ACU는 부하에 따라 하루에도 크게 오르내려
원시 표본 회귀는 일중 주기에 휘둘린다. 대신 **일별 peak ACU**로 추세를 잡고,
두 가지 경고를 낸다: (1) 일별 peak가 이미 max ACU의 대부분을 며칠째 차지하면
"스케일 헤드룸 없음"(이미 천장), (2) peak가 상승 추세면 max ACU 도달 ETA를
예측. 한계 = cluster_meta.serverlessv2_max_acu.

캐시 전용(metric_snapshots·cluster_meta·cluster_settings) — cost_check /
param_fitness와 동일 패턴. run_ts를 공유해 같은 사이클 finding과 한 배치로
대시보드에 잡힌다.
"""

import json
from datetime import datetime, timezone

# Aurora 클러스터 볼륨 상한(128 TiB) — 추정이 아닌 실제 플랫폼 한계.
_AURORA_MAX_STORAGE_BYTES = 128 * 1024 ** 4

ALERT_DAYS = 14          # 이 일수 이내 도달 예상이면 경보
MIN_SAMPLES = 20         # 추세 신뢰를 위한 최소 표본
SEV_CRIT_DAYS = 3
SEV_WARN_DAYS = 7

# ACU 예측 — 부하에 휘둘리는 원시 표본 대신 일별 peak로 천장 접근을 본다.
ACU_SAT_FRAC = 0.95      # 일별 peak가 max ACU의 95% 이상이면 "포화"로 카운트
ACU_MIN_DAYS = 3         # 일별 peak가 최소 3일치는 있어야 추세를 믿는다
ACU_SAT_DAYS_FRAC = 0.6  # 관측일의 60% 이상이 포화면 "이미 천장" 경고

# (metric_type, 한국어 라벨, 한계 해석 함수 키)
_FORECAST_METRICS = [
    ("storage_bytes", "스토리지", "storage"),
    ("db_connections", "커넥션", "connections"),
]


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
        sql=f"/* source=dbops-capforecast */ {sql}", parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
        out.append(row)
    return out


def _fmt(metric_key, v):
    if metric_key == "storage":
        return f"{v / 1024 ** 3:.1f}GB"
    if metric_key == "acu":
        return f"{v:.1f} ACU"
    return f"{int(v)}"


def collect_capacity_forecast(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, snapshot_ts=None, days_lookback=7, engine=""):
    ts = snapshot_ts or datetime.now(timezone.utc).isoformat()
    is_mysql = "mysql" in (engine or "").lower()

    meta = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT max_connections, serverlessv2_max_acu FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    max_conn = None
    max_acu = None
    if meta:
        if meta[0].get("max_connections") is not None:
            try:
                max_conn = float(meta[0]["max_connections"])
            except (TypeError, ValueError):
                max_conn = None
        if meta[0].get("serverlessv2_max_acu") is not None:
            try:
                max_acu = float(meta[0]["serverlessv2_max_acu"])
            except (TypeError, ValueError):
                max_acu = None
    # cluster_meta가 비어 있으면 cluster_settings의 max_connections로 폴백.
    if not max_conn:
        cfg = _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "SELECT value FROM cluster_settings WHERE cluster_id = :cid "
            "AND name = 'max_connections' ORDER BY updated_at DESC LIMIT 1",
            {"cid": cluster_id},
        )
        if cfg and cfg[0].get("value"):
            try:
                max_conn = float(cfg[0]["value"])
            except (TypeError, ValueError):
                max_conn = None

    findings = []
    for metric_type, label, key in _FORECAST_METRICS:
        if key == "connections" and not max_conn:
            continue  # 한계를 모르면 ETA 계산 불가 — 침묵
        rows = _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "SELECT REGR_SLOPE(value::float, EXTRACT(EPOCH FROM ts) / 86400) AS slope, "
            "       (array_agg(value ORDER BY ts DESC))[1] AS latest, "
            "       COUNT(*) AS samples "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid AND metric_type = :mt "
            "  AND ts > NOW() - (:days || ' days')::interval "
            "  AND (dimensions IS NULL OR dimensions::text = '{}')",
            {"cid": cluster_id, "mt": metric_type, "days": str(days_lookback)},
        )
        row = rows[0] if rows else {}
        slope = float(row.get("slope") or 0)
        current = float(row.get("latest") or 0)
        samples = int(row.get("samples") or 0)
        limit = _AURORA_MAX_STORAGE_BYTES if key == "storage" else max_conn

        if samples < MIN_SAMPLES or slope <= 0 or current >= limit:
            continue  # 증가 추세 아니거나 표본 부족 → 경보 안 함
        days_until = int((limit - current) / slope)
        if days_until > ALERT_DAYS:
            continue

        severity = (
            "critical" if days_until <= SEV_CRIT_DAYS
            else "warning" if days_until <= SEV_WARN_DAYS
            else "info"
        )
        usage_pct = current / limit * 100
        findings.append({
            "check_type": "capacity_forecast",
            "severity": severity,
            "subject": label,
            "value_str": f"{_fmt(key, current)} / {_fmt(key, limit)} ({usage_pct:.1f}%)",
            "threshold_str": f"{ALERT_DAYS}일 이내 도달 예상",
            "recommendation": (
                f"현 증가 추세({_fmt(key, slope)}/일)로는 약 {days_until}일 후 "
                f"{label} 한계({_fmt(key, limit)})에 도달할 것으로 예측됩니다. "
                + (
                    (
                        "max_connections 상향 또는 연결 풀링"
                        + ("(RDS Proxy/ProxySQL)" if is_mysql else "(PgBouncer/RDS Proxy)")
                        + "을 검토하세요. "
                    )
                    if key == "connections" else
                    "Aurora 스토리지는 자동 확장되지만 128 TiB가 하드 상한입니다 — "
                    "데이터 증가 원인(미사용 테이블·로그 누적)을 점검하세요. "
                )
                + f"추세는 최근 {days_lookback}일 선형 회귀 기반이라 워크로드 변화 시 달라질 수 있습니다."
            ),
            "details": json.dumps({
                "metric": metric_type, "current": round(current, 2),
                "limit": round(limit, 2), "slope_per_day": round(slope, 4),
                "days_until_limit": days_until, "samples": samples,
                "usage_pct": round(usage_pct, 1),
            }),
        })

    # === ACU 고갈 예측 (Serverless v2 전용) ===
    # ACU는 부하에 따라 하루에도 크게 진동하므로 원시 표본이 아니라 일별 peak로
    # 추세를 본다. max ACU(serverlessv2_max_acu)를 모르면(프로비저닝 등) 침묵.
    if max_acu:
        acu_rows = _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "SELECT REGR_SLOPE(peak, day_num) AS slope, "
            "       (array_agg(peak ORDER BY day_num DESC))[1] AS latest_peak, "
            "       MAX(peak) AS max_peak, COUNT(*) AS days, "
            "       SUM(CASE WHEN peak >= :sat THEN 1 ELSE 0 END) AS sat_days "
            "FROM ("
            "  SELECT floor(EXTRACT(EPOCH FROM date_trunc('day', ts)) / 86400) AS day_num, "
            "         MAX(value::float) AS peak "
            "  FROM metric_snapshots "
            "  WHERE cluster_id = :cid AND metric_type = 'serverless_acu' "
            "    AND ts > NOW() - (:days || ' days')::interval "
            "    AND (dimensions IS NULL OR dimensions::text = '{}') "
            "  GROUP BY 1"
            ") d",
            {"cid": cluster_id, "sat": max_acu * ACU_SAT_FRAC, "days": str(days_lookback)},
        )
        arow = acu_rows[0] if acu_rows else {}
        acu_slope = float(arow.get("slope") or 0)
        latest_peak = float(arow.get("latest_peak") or 0)
        acu_days = int(arow.get("days") or 0)
        sat_days = int(arow.get("sat_days") or 0)

        if acu_days >= ACU_MIN_DAYS:
            sat_ratio = sat_days / acu_days
            if sat_ratio >= ACU_SAT_DAYS_FRAC:
                # (1) 이미 천장 — 며칠째 일별 peak가 max ACU 근처. 스케일 헤드룸 없음.
                findings.append({
                    "check_type": "capacity_forecast",
                    "severity": "critical",
                    "subject": "ACU",
                    "value_str": f"일별 peak {latest_peak:.1f} / max {max_acu:.1f} ACU",
                    "threshold_str": f"{acu_days}일 중 {sat_days}일 max의 {ACU_SAT_FRAC*100:.0f}% 도달",
                    "recommendation": (
                        f"최근 {acu_days}일 중 {sat_days}일의 일별 peak ACU가 설정된 max "
                        f"{max_acu:.1f} ACU의 {ACU_SAT_FRAC*100:.0f}% 이상에 도달했습니다 — "
                        f"Serverless v2가 더 이상 위로 스케일할 헤드룸이 거의 없어, 수요가 더 "
                        f"몰리면 성능 저하(쿼리 지연·연결 대기)로 이어집니다. "
                        f"serverlessv2_max_acu 상향을 검토하세요. ACU 상한은 비용 상한이기도 "
                        f"하므로 Cost 탭의 ACU 사용 추이와 함께 판단하세요."
                    ),
                    "details": json.dumps({
                        "metric": "serverless_acu", "latest_daily_peak": round(latest_peak, 2),
                        "max_acu": max_acu, "observed_days": acu_days,
                        "saturated_days": sat_days, "saturation_frac": round(sat_ratio, 2),
                        "case": "ceiling_reached",
                    }),
                })
            elif acu_slope > 0 and latest_peak < max_acu:
                # (2) 추세 상승 — 일별 peak가 오르며 max ACU 도달 ETA가 임박.
                days_until = int((max_acu - latest_peak) / acu_slope)
                if 0 <= days_until <= ALERT_DAYS:
                    severity = (
                        "critical" if days_until <= SEV_CRIT_DAYS
                        else "warning" if days_until <= SEV_WARN_DAYS
                        else "info"
                    )
                    usage_pct = latest_peak / max_acu * 100
                    findings.append({
                        "check_type": "capacity_forecast",
                        "severity": severity,
                        "subject": "ACU",
                        "value_str": f"일별 peak {latest_peak:.1f} / max {max_acu:.1f} ACU ({usage_pct:.1f}%)",
                        "threshold_str": f"{ALERT_DAYS}일 이내 max ACU 도달 예상",
                        "recommendation": (
                            f"일별 peak ACU가 상승 추세(약 {acu_slope:.2f} ACU/일)로, 현 추세면 약 "
                            f"{days_until}일 후 설정된 max {max_acu:.1f} ACU에 도달할 것으로 "
                            f"예측됩니다. 천장에 닿으면 Serverless v2가 더 스케일하지 못해 부하가 "
                            f"성능 저하로 전가됩니다. serverlessv2_max_acu 상향을 미리 검토하세요. "
                            f"추세는 최근 {days_lookback}일 일별 peak 회귀 기반이라 워크로드 변화 시 "
                            f"달라질 수 있습니다."
                        ),
                        "details": json.dumps({
                            "metric": "serverless_acu", "latest_daily_peak": round(latest_peak, 2),
                            "max_acu": max_acu, "slope_per_day": round(acu_slope, 4),
                            "days_until_limit": days_until, "observed_days": acu_days,
                            "usage_pct": round(usage_pct, 1), "case": "trending_up",
                        }),
                    })

    for f in findings:
        _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "INSERT INTO cluster_health_findings "
            "(cluster_id, snapshot_time, check_type, severity, subject, value_str, threshold_str, recommendation, details) "
            "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, :value_str, :threshold_str, :recommendation, :details::jsonb)",
            {"cluster_id": cluster_id, "ts": ts, "check_type": f["check_type"],
             "severity": f["severity"], "subject": f["subject"], "value_str": f["value_str"],
             "threshold_str": f["threshold_str"], "recommendation": f["recommendation"],
             "details": f["details"]},
        )

    return {"cluster_id": cluster_id, "max_connections": max_conn,
            "max_acu": max_acu, "findings_emitted": len(findings)}
