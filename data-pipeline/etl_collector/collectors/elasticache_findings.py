"""ElastiCache Findings Collector — eviction spike, low hit-rate, memory pressure,
replication lag, high CPU, connection surge.

Reads the cached metric_snapshots only (no live AWS). Writes elasticache_* rows
to cluster_health_findings, all sharing the handler's snapshot_ts. Memcached
skips replication-lag + memory-pressure and uses get_hits/get_misses for hit-rate.
All metric_snapshots rows are cluster-level (dimensions='{}')."""

import json
from datetime import datetime, timezone

EVICTIONS_WARNING = 100.0
EVICTIONS_CRITICAL = 1000.0
HIT_RATE_WARNING = 0.85
HIT_RATE_CRITICAL = 0.70
MIN_HIT_SAMPLES = 20
MEMORY_WARNING_PCT = 85.0
MEMORY_CRITICAL_PCT = 95.0
REPL_LAG_WARNING_MS = 100.0
REPL_LAG_CRITICAL_MS = 1000.0
CPU_WARNING_PCT = 80.0
CPU_CRITICAL_PCT = 90.0
CONN_SURGE_WARNING = 60000.0


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
        sql=f"/* source=dbops-ecfind */ {sql}", parameters=sql_params,
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


def collect_elasticache_findings(
    rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
    cluster_id, snapshot_ts=None, window_hours=1,
):
    ts = snapshot_ts or datetime.now(timezone.utc).isoformat()
    errors = []

    # engine → hit-rate metric keys + which rules apply
    engine = "redis"
    try:
        meta = _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
        if meta and meta[0].get("engine"):
            engine = str(meta[0]["engine"]).lower()
    except Exception as e:
        errors.append(f"meta: {e}")
    is_memcached = engine == "memcached"

    try:
        agg = _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "SELECT "
            "  SUM(CASE WHEN metric_type='evictions' THEN value ELSE 0 END) AS sum_evictions, "
            "  SUM(CASE WHEN metric_type='cache_hits' THEN value ELSE 0 END) AS sum_cache_hits, "
            "  SUM(CASE WHEN metric_type='cache_misses' THEN value ELSE 0 END) AS sum_cache_misses, "
            "  SUM(CASE WHEN metric_type='get_hits' THEN value ELSE 0 END) AS sum_get_hits, "
            "  SUM(CASE WHEN metric_type='get_misses' THEN value ELSE 0 END) AS sum_get_misses, "
            "  MAX(CASE WHEN metric_type='memory_usage_pct' THEN value END) AS max_memory_pct, "
            "  MAX(CASE WHEN metric_type='replication_lag' THEN value END) AS max_replication_lag, "
            "  MAX(CASE WHEN metric_type='engine_cpu' THEN value END) AS max_engine_cpu, "
            "  MAX(CASE WHEN metric_type='cache_cpu' THEN value END) AS max_cache_cpu, "
            "  MAX(CASE WHEN metric_type='curr_connections' THEN value END) AS max_curr_connections, "
            "  COUNT(CASE WHEN metric_type IN ('cache_hits','get_hits') THEN 1 END) AS hit_samples "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid "
            "  AND ts > NOW() - (:hours || ' hours')::interval "
            "  AND (dimensions IS NULL OR dimensions::text = '{}')",
            {"cid": cluster_id, "hours": str(window_hours)},
        )
    except Exception as e:
        errors.append(f"agg: {e}")
        return {"cluster_id": cluster_id, "findings_emitted": 0, "errors": errors}
    if not agg:
        return {"cluster_id": cluster_id, "findings_emitted": 0, "errors": errors}
    r = agg[0]

    def _f(key):
        v = r.get(key)
        return float(v) if v is not None else None

    findings = []

    def add(check_type, severity, subject, value_str, threshold_str, recommendation, details):
        findings.append({
            "check_type": check_type, "severity": severity, "subject": subject,
            "value_str": value_str, "threshold_str": threshold_str,
            "recommendation": recommendation, "details": json.dumps(details),
        })

    # Rule 1: eviction spike
    ev = _f("sum_evictions") or 0.0
    if ev > EVICTIONS_WARNING:
        sev = "critical" if ev > EVICTIONS_CRITICAL else "warning"
        add("elasticache_evictions_spike", sev, "ElastiCache Eviction Spike",
            f"evictions {int(ev)}건 / {window_hours}시간",
            f"evictions > {int(EVICTIONS_CRITICAL)}건" if sev == "critical" else f"evictions > {int(EVICTIONS_WARNING)}건",
            f"최근 {window_hours}시간 eviction이 {int(ev)}건 발생했습니다. 메모리 용량 증설 또는 maxmemory-policy(LRU/TTL) 재검토를 권장합니다.",
            {"sum_evictions": ev, "window_hours": window_hours})

    # Rule 2: low hit-rate (engine-branched keys)
    if is_memcached:
        hits, misses = _f("sum_get_hits") or 0.0, _f("sum_get_misses") or 0.0
    else:
        hits, misses = _f("sum_cache_hits") or 0.0, _f("sum_cache_misses") or 0.0
    samples = int(r.get("hit_samples") or 0)
    total = hits + misses
    if samples >= MIN_HIT_SAMPLES and total > 0:
        hr = hits / total
        if hr < HIT_RATE_WARNING:
            sev = "critical" if hr < HIT_RATE_CRITICAL else "warning"
            pct = round(hr * 100, 1)
            add("elasticache_low_hit_rate", sev, "ElastiCache Low Hit Rate",
                f"hit rate {pct}%",
                f"hit rate < {int(HIT_RATE_CRITICAL*100)}%" if sev == "critical" else f"hit rate < {int(HIT_RATE_WARNING*100)}%",
                f"최근 {window_hours}시간 cache hit rate가 {pct}%입니다. 캐시 키 설계·TTL·워킹셋 크기 또는 메모리 증설을 점검하세요.",
                {"hit_rate": round(hr, 4), "hits": hits, "misses": misses, "window_hours": window_hours})

    # Rule 3: memory pressure (Redis/Valkey only)
    if not is_memcached:
        mem = _f("max_memory_pct")
        if mem is not None and mem >= MEMORY_WARNING_PCT:
            sev = "critical" if mem >= MEMORY_CRITICAL_PCT else "warning"
            add("elasticache_memory_pressure", sev, "ElastiCache Memory Pressure",
                f"memory {mem:.1f}%",
                f"memory ≥ {int(MEMORY_CRITICAL_PCT)}%" if sev == "critical" else f"memory ≥ {int(MEMORY_WARNING_PCT)}%",
                f"최근 {window_hours}시간 메모리 사용률 peak이 {mem:.1f}%입니다. eviction/OOM 위험 — 노드 타입 상향 또는 샤드 추가를 권장합니다.",
                {"max_memory_usage_pct": mem, "window_hours": window_hours})

    # Rule 4: replication lag (Redis/Valkey only)
    if not is_memcached:
        lag = _f("max_replication_lag")
        if lag is not None and lag >= REPL_LAG_WARNING_MS:
            sev = "critical" if lag >= REPL_LAG_CRITICAL_MS else "warning"
            add("elasticache_replication_lag", sev, "ElastiCache Replication Lag",
                f"peak {lag:.0f} ms",
                f"replication lag ≥ {int(REPL_LAG_CRITICAL_MS)} ms" if sev == "critical" else f"replication lag ≥ {int(REPL_LAG_WARNING_MS)} ms",
                f"최근 {window_hours}시간 replication lag peak이 {lag:.0f} ms입니다. 쓰기 부하 완화 또는 리드 레플리카 확장을 점검하세요.",
                {"max_replication_lag_ms": lag, "window_hours": window_hours})

    # Rule 5: high CPU (prefer engine_cpu — Redis single-threaded bottleneck)
    cpu = _f("max_engine_cpu")
    cpu_label = "engine CPU"
    if cpu is None:
        cpu, cpu_label = _f("max_cache_cpu"), "CPU"
    if cpu is not None and cpu >= CPU_WARNING_PCT:
        sev = "critical" if cpu >= CPU_CRITICAL_PCT else "warning"
        add("elasticache_high_cpu", sev, "ElastiCache High CPU",
            f"{cpu_label} {cpu:.1f}%",
            f"{cpu_label} ≥ {int(CPU_CRITICAL_PCT)}%" if sev == "critical" else f"{cpu_label} ≥ {int(CPU_WARNING_PCT)}%",
            f"최근 {window_hours}시간 {cpu_label} peak이 {cpu:.1f}%입니다. 핫 키·비싼 명령(KEYS/SORT) 점검 또는 노드 타입 상향을 권장합니다.",
            {"max_cpu_pct": cpu, "cpu_metric": cpu_label, "window_hours": window_hours})

    # Rule 6: connection surge
    conn = _f("max_curr_connections")
    if conn is not None and conn > CONN_SURGE_WARNING:
        add("elasticache_connection_surge", "warning", "ElastiCache Connection Surge",
            f"peak {int(conn)} connections",
            f"connections > {int(CONN_SURGE_WARNING)}",
            f"최근 {window_hours}시간 연결 수 peak이 {int(conn)}개입니다(Redis 한도 65000). connection pooling·클라이언트 누수 점검을 권장합니다.",
            {"max_curr_connections": conn, "window_hours": window_hours})

    for f in findings:
        _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "INSERT INTO cluster_health_findings "
            "(cluster_id, snapshot_time, check_type, severity, subject, "
            "value_str, threshold_str, recommendation, details) "
            "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, "
            ":value_str, :threshold_str, :recommendation, :details::jsonb)",
            {"cluster_id": cluster_id, "ts": ts, "check_type": f["check_type"],
             "severity": f["severity"], "subject": f["subject"], "value_str": f["value_str"],
             "threshold_str": f["threshold_str"], "recommendation": f["recommendation"],
             "details": f["details"]},
        )

    return {"cluster_id": cluster_id, "engine": engine,
            "findings_emitted": len(findings), "errors": errors}
