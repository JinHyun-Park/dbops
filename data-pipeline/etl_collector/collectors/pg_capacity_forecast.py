"""고갈 예측 경보 — storage/connection이 한계에 도달하는 ETA를 사전 경고.

대시보드의 Capacity Forecast 패널(_capacity_forecast)은 DBA가 직접 메트릭을
골라 봐야 보인다. 이 collector는 그 예측을 매 수집 사이클에 자동으로 돌려,
한계 도달이 임박(기본 14일 이내)하면 finding으로 띄운다 — DBA가 패널을
들여다보지 않아도 "현 추세로 N일 후 connection 한도" 경고가 Maintenance
Health에 능동적으로 올라온다.

선형 추세(REGR_SLOPE)를 실제 한계(connection=cluster_meta.max_connections,
storage=Aurora 볼륨 상한 128 TiB)와 비교해 days_until을 계산한다. 추세가
증가하지 않거나(slope≤0) 표본이 부족하면 침묵한다 — 노이즈로 거짓 경보를
내지 않는다. ETA가 가까울수록 심각도를 높인다(≤3일 critical, ≤7일 warning,
≤14일 info).

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
    return f"{int(v)}"


def collect_capacity_forecast(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, snapshot_ts=None, days_lookback=7):
    ts = snapshot_ts or datetime.now(timezone.utc).isoformat()

    meta = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT max_connections FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    max_conn = None
    if meta and meta[0].get("max_connections") is not None:
        try:
            max_conn = float(meta[0]["max_connections"])
        except (TypeError, ValueError):
            max_conn = None
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
                    "max_connections 상향 또는 연결 풀링(PgBouncer/RDS Proxy)을 검토하세요. "
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

    return {"cluster_id": cluster_id, "max_connections": max_conn, "findings_emitted": len(findings)}
