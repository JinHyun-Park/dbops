"""Query latency-regression findings — catch queries whose plan likely flipped.

pganalyze-style plan-history needs auto-EXPLAIN of every top query (param
handling + planner load on the target). We get most of that value cheaply from
data already in the cache: query_stats stores per-query (calls, total_time_ms)
each ETL run for PG (pg_stat_statements) AND MySQL (the mysql stats collector).

pg_stat_statements' mean_exec_time is the LIFETIME average — a regression gets
diluted by history — so we compute the per-INTERVAL mean from consecutive
snapshots (Δtotal_time / Δcalls) and flag a query whose recent interval mean is
>= 2x its median interval mean over the lookback. No EXPLAIN, no target-DB load.

ponytail: median baseline over a 24h window; if noise shows up, widen the window
or require N consecutive regressed intervals. Plan-HASH capture (confirm it's a
plan flip vs data growth) is the heavier upgrade — add when the latency signal
alone isn't enough.
"""

LOOKBACK_HOURS = 24
MIN_RATIO = 2.0          # recent >= 2x baseline
MIN_RECENT_MS = 10.0     # ignore sub-10ms noise
MIN_CALLS = 20           # ignore rarely-run queries
CRIT_RATIO = 5.0
SUBJECT_MAX = 120

REGRESSION_SQL = f"""
WITH ordered AS (
  SELECT query_hash, query_text, snapshot_time, calls, total_time_ms,
         LAG(calls) OVER w AS prev_calls,
         LAG(total_time_ms) OVER w AS prev_total
  FROM query_stats
  WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '{LOOKBACK_HOURS} hours'
  WINDOW w AS (PARTITION BY query_hash ORDER BY snapshot_time)
),
intervals AS (
  SELECT query_hash, query_text, snapshot_time,
         (calls - prev_calls) AS d_calls,
         CASE WHEN (calls - prev_calls) > 0 AND (total_time_ms - prev_total) >= 0
              THEN (total_time_ms - prev_total) / (calls - prev_calls) END AS interval_mean
  FROM ordered
  WHERE prev_calls IS NOT NULL
),
agg AS (
  SELECT query_hash,
         max(query_text) AS query_text,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY interval_mean)
           FILTER (WHERE interval_mean IS NOT NULL) AS baseline_mean,
         (array_agg(interval_mean ORDER BY snapshot_time DESC)
           FILTER (WHERE interval_mean IS NOT NULL))[1] AS recent_mean,
         COALESCE(sum(d_calls) FILTER (WHERE interval_mean IS NOT NULL), 0) AS total_calls
  FROM intervals
  GROUP BY query_hash
)
SELECT query_hash, query_text, baseline_mean, recent_mean, total_calls
FROM agg
WHERE recent_mean IS NOT NULL AND baseline_mean IS NOT NULL AND baseline_mean > 0
  AND recent_mean >= :ratio * baseline_mean
  AND recent_mean >= :min_recent
  AND total_calls >= :min_calls
ORDER BY (recent_mean - baseline_mean) DESC
LIMIT 10
"""

INSERT_FINDING = (
    "INSERT INTO cluster_health_findings "
    "(cluster_id, snapshot_time, check_type, severity, subject, value_str, "
    " threshold_str, recommendation, details) "
    "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, "
    " :value_str, :threshold_str, :recommendation, :details::jsonb)"
)


def _execute(rds_data, cluster_arn, secret_arn, db_name, sql, params=None):
    sql_params = []
    for k, v in (params or {}).items():
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
        sql=f"/* source=dbops-etl */ {sql}", parameters=sql_params,
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
            else:
                row[col] = next((f[t] for t in ("stringValue", "longValue", "doubleValue", "booleanValue") if t in f), None)
        out.append(row)
    return out


def collect_query_regression(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, snapshot_ts):
    rows = _execute(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
                    REGRESSION_SQL, {"cid": cluster_id, "ratio": MIN_RATIO,
                                     "min_recent": MIN_RECENT_MS, "min_calls": MIN_CALLS})
    findings = 0
    for r in rows:
        recent = float(r["recent_mean"])
        baseline = float(r["baseline_mean"])
        ratio = recent / baseline if baseline else 0
        subject = (r.get("query_text") or "")[:SUBJECT_MAX] or r.get("query_hash", "?")
        _execute(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, INSERT_FINDING, {
            "cluster_id": cluster_id, "ts": snapshot_ts, "check_type": "query_regression",
            "severity": "critical" if ratio >= CRIT_RATIO else "warning",
            "subject": subject,
            "value_str": f"{recent:.0f}ms (×{ratio:.1f})",
            "threshold_str": f"기준 {baseline:.0f}ms",
            "recommendation": (
                "쿼리 구간 평균 실행시간이 기준 대비 크게 느려졌습니다 — 플랜 리그레션 또는 "
                "데이터 증가가 의심됩니다. Query Lab에서 EXPLAIN으로 현재 플랜을 확인하고, "
                "필요하면 인덱스/통계(ANALYZE)를 점검하세요."),
            "details": "{}",
        })
        findings += 1
    return {"cluster_id": cluster_id, "findings": findings, "checked": len(rows)}
