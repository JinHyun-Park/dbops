import json
import os
import re
import traceback
import boto3


def _parse_int(value, default, min_v=1, max_v=168):
    try:
        return max(min_v, min(int(value), max_v))
    except (ValueError, TypeError):
        return default


def _parse_float(value, default):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


CLUSTER_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,63}$")


def _rds_data():
    return boto3.client("rds-data")


def _make_query(rds_data, cluster_arn, secret_arn, database):
    def query(sql, params=None):
        sql_params = []
        if params:
            for k, v in params.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    sql_params.append({"name": k, "value": {"longValue": v}})
                elif isinstance(v, float):
                    sql_params.append({"name": k, "value": {"doubleValue": v}})
                else:
                    sql_params.append({"name": k, "value": {"stringValue": str(v)}})
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=f"/* source=dbops-dashboard */ {sql}",
            parameters=sql_params,
            includeResultMetadata=True,
        )
        cols = [c["name"] for c in resp.get("columnMetadata", [])]
        rows = []
        for rec in resp.get("records", []):
            row = {}
            for i, f in enumerate(rec):
                col = cols[i] if i < len(cols) else f"col_{i}"
                if f.get("isNull"):
                    row[col] = None
                    continue
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        row[col] = f[typ]
                        break
                else:
                    row[col] = None
            rows.append(row)
        return rows
    return query


_CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE", "")


def _lookup_cluster(cluster_id: str) -> dict:
    """Resolve cluster_arn / secret_arn / db_name from the DynamoDB clusters
    registry — needed when an endpoint queries the live target cluster
    (e.g. listing indexes) instead of the cache DB."""
    if not cluster_id or not _CLUSTERS_TABLE_NAME:
        return {}
    try:
        table = boto3.resource("dynamodb").Table(_CLUSTERS_TABLE_NAME)
        return table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
    except Exception as e:
        print(f"[dashboard] cluster lookup failed for {cluster_id}: {e}")
        return {}


def _table_indexes(cluster_id: str, schema: str, table_name: str) -> dict:
    """List every index on a given table (definition, size, scan count,
    uniqueness, primary-key flag). Engine-aware: PG queries pg_stat_user_indexes,
    MySQL aggregates from information_schema.statistics + table_io_waits_summary."""
    if not schema or not table_name:
        return {"error": "schema and table required"}
    cluster = _lookup_cluster(cluster_id)
    if not cluster:
        return {"error": f"cluster {cluster_id!r} not registered"}
    cluster_arn = cluster.get("cluster_arn")
    secret_arn = cluster.get("secret_arn")
    db_name = cluster.get("db_name") or "postgres"
    engine = (cluster.get("engine") or "").lower()
    if not cluster_arn or not secret_arn:
        return {"error": "cluster registry missing cluster_arn/secret_arn"}

    if "mysql" in engine:
        # MySQL: information_schema.statistics holds per-column index info;
        # we collapse to one row per index (GROUP_CONCAT columns into the
        # definition column) and join performance_schema.table_io_waits_summary_by_index_usage
        # for usage counts.
        sql = (
            "SELECT "
            "  s.INDEX_NAME AS index_name, "
            "  CONCAT('USING ', MAX(s.INDEX_TYPE), ' (', GROUP_CONCAT(s.COLUMN_NAME ORDER BY s.SEQ_IN_INDEX), ')') AS definition, "
            "  COALESCE(MAX(stat.STAT_VALUE * stat.STAT_VALUE), 0) AS bytes, "  # rough estimate
            "  COALESCE(MAX(ios.COUNT_FETCH), 0) AS idx_scan, "
            "  COALESCE(MAX(ios.COUNT_READ), 0) AS idx_tup_read, "
            "  (MAX(s.NON_UNIQUE) = 0) AS is_unique, "
            "  (MAX(s.INDEX_NAME) = 'PRIMARY') AS is_primary, "
            "  TRUE AS is_valid "
            "FROM information_schema.statistics s "
            "LEFT JOIN performance_schema.table_io_waits_summary_by_index_usage ios "
            "  ON ios.OBJECT_SCHEMA = s.TABLE_SCHEMA AND ios.OBJECT_NAME = s.TABLE_NAME AND ios.INDEX_NAME = s.INDEX_NAME "
            "LEFT JOIN mysql.innodb_index_stats stat "
            "  ON stat.database_name = s.TABLE_SCHEMA AND stat.table_name = s.TABLE_NAME AND stat.index_name = s.INDEX_NAME "
            "  AND stat.stat_name = 'size' "
            "WHERE s.TABLE_SCHEMA = :s AND s.TABLE_NAME = :t "
            "GROUP BY s.INDEX_NAME "
            "ORDER BY is_primary DESC, index_name"
        )
    else:
        sql = (
            "SELECT "
            "  i.indexrelname AS index_name, "
            "  pg_get_indexdef(i.indexrelid) AS definition, "
            "  pg_relation_size(i.indexrelid)::bigint AS bytes, "
            "  i.idx_scan, "
            "  i.idx_tup_read, "
            "  ix.indisunique AS is_unique, "
            "  ix.indisprimary AS is_primary, "
            "  ix.indisvalid AS is_valid "
            "FROM pg_stat_user_indexes i "
            "JOIN pg_index ix ON ix.indexrelid = i.indexrelid "
            "WHERE i.schemaname = :s AND i.relname = :t "
            "ORDER BY pg_relation_size(i.indexrelid) DESC"
        )
    rds_data = boto3.client("rds-data")
    try:
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=db_name,
            sql=f"/* source=dbops-dashboard-indexes */ {sql}",
            parameters=[
                {"name": "s", "value": {"stringValue": schema}},
                {"name": "t", "value": {"stringValue": table_name}},
            ],
            includeResultMetadata=True,
        )
    except Exception as e:
        return {"error": "execution_failed", "message": str(e)[:300]}

    # MySQL Data API leaves `name` blank for computed/aliased columns; the
    # alias ends up in `label`. Prefer whichever is non-empty.
    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    rows = []
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
        rows.append(row)
    return {"schema": schema, "table": table_name, "indexes": rows}


_ALLOWED_ORIGINS = {
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
}

_CURRENT_ORIGIN = {"value": ""}


def _set_origin(event):
    headers = (event or {}).get("headers") or {}
    _CURRENT_ORIGIN["value"] = headers.get("origin") or headers.get("Origin") or ""


def _response(status, body):
    origin = _CURRENT_ORIGIN["value"]
    if _ALLOWED_ORIGINS:
        allow = origin if origin in _ALLOWED_ORIGINS else ""
    else:
        allow = origin or "*"
    cors = {}
    if allow:
        cors = {"Access-Control-Allow-Origin": allow}
        if allow != "*":
            cors["Vary"] = "Origin"
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            **cors,
        },
        "body": json.dumps(body, default=str),
    }


def _overview(query, cluster_id):
    meta = query(
        "SELECT * FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    recent_metrics = query(
        "SELECT metric_type, AVG(value) as avg_val, MAX(value) as max_val "
        "FROM metric_snapshots WHERE cluster_id = :cid AND ts > NOW() - INTERVAL '1 hour' "
        "GROUP BY metric_type",
        {"cid": cluster_id},
    )
    top_queries = query(
        "SELECT query_hash, query_text, calls, total_time_ms, mean_time_ms "
        "FROM query_stats WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
        "ORDER BY total_time_ms DESC LIMIT 10",
        {"cid": cluster_id},
    )
    recent_events = query(
        "SELECT id, event_time as ts, event_type, severity, source, message, raw_event "
        "FROM event_log WHERE cluster_id = :cid "
        "ORDER BY event_time DESC LIMIT 10",
        {"cid": cluster_id},
    )
    return {
        "cluster": meta[0] if meta else None,
        "metrics": recent_metrics,
        "top_queries": top_queries,
        "events": recent_events,
    }


def _timeseries(query, cluster_id, metric_type, hours):
    rows = query(
        "SELECT ts, value, dimensions::text as dimensions "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "AND metric_type = :mt "
        "AND ts > NOW() - (:hours || ' hours')::interval "
        "ORDER BY ts ASC",
        {"cid": cluster_id, "mt": metric_type, "hours": str(hours)},
    )
    return {"cluster_id": cluster_id, "metric_type": metric_type, "hours": hours, "points": rows}


def _slow_queries(query, cluster_id, hours, threshold_ms):
    rows = query(
        "SELECT query_hash, query_text, calls, total_time_ms, mean_time_ms, rows_returned "
        "FROM query_stats "
        "WHERE cluster_id = :cid "
        "AND snapshot_time > NOW() - (:hours || ' hours')::interval "
        "AND mean_time_ms >= :threshold "
        "ORDER BY mean_time_ms DESC "
        "LIMIT 20",
        {"cid": cluster_id, "hours": str(hours), "threshold": float(threshold_ms)},
    )
    return {"cluster_id": cluster_id, "hours": hours, "threshold_ms": threshold_ms, "slow_queries": rows}


def _query_detail(query, cluster_id, query_hash):
    rows = query(
        "SELECT snapshot_time, calls, total_time_ms, mean_time_ms, rows_returned, "
        "shared_blks_hit, shared_blks_read, query_text "
        "FROM query_stats "
        "WHERE cluster_id = :cid AND query_hash = :qh "
        "ORDER BY snapshot_time DESC LIMIT 100",
        {"cid": cluster_id, "qh": query_hash},
    )
    return {"cluster_id": cluster_id, "query_hash": query_hash, "snapshots": rows}


METRIC_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,49}$")


def _batch_timeseries(query, cluster_id, metric_names, hours):
    metric_names = [m for m in metric_names if METRIC_NAME_RE.match(m)][:20]
    if not metric_names:
        return {"cluster_id": cluster_id, "hours": hours, "series": {}}

    placeholders = ", ".join(f":m{i}" for i in range(len(metric_names)))
    params = {"cid": cluster_id, "hours": str(hours)}
    for i, m in enumerate(metric_names):
        params[f"m{i}"] = m

    rows = query(
        f"SELECT ts, metric_type, value, dimensions::text as dimensions "
        f"FROM metric_snapshots "
        f"WHERE cluster_id = :cid "
        f"AND metric_type IN ({placeholders}) "
        f"AND ts > NOW() - (:hours || ' hours')::interval "
        f"ORDER BY ts ASC",
        params,
    )

    series = {m: [] for m in metric_names}
    for r in rows:
        mt = r.get("metric_type")
        if mt in series:
            series[mt].append({"ts": r["ts"], "value": r["value"], "dimensions": r.get("dimensions")})
    return {"cluster_id": cluster_id, "hours": hours, "series": series}


def _multi_cluster_overview(query):
    rows = query(
        "WITH latest_metrics AS ("
        "  SELECT cluster_id, metric_type, "
        "    (array_agg(value ORDER BY ts DESC))[1] AS latest_value "
        "  FROM metric_snapshots "
        "  WHERE ts > NOW() - INTERVAL '15 minutes' "
        "  AND metric_type IN ('cpu', 'aas', 'conn_active', 'conn_idle', 'storage_bytes', 'deadlocks') "
        "  GROUP BY cluster_id, metric_type"
        "), "
        "agg AS ("
        "  SELECT "
        "    cluster_id, "
        "    MAX(CASE WHEN metric_type='cpu' THEN latest_value END) AS cpu, "
        "    MAX(CASE WHEN metric_type='aas' THEN latest_value END) AS aas, "
        "    MAX(CASE WHEN metric_type='conn_active' THEN latest_value END) AS conn_active, "
        "    MAX(CASE WHEN metric_type='conn_idle' THEN latest_value END) AS conn_idle, "
        "    MAX(CASE WHEN metric_type='storage_bytes' THEN latest_value END) AS storage_bytes, "
        "    MAX(CASE WHEN metric_type='deadlocks' THEN latest_value END) AS deadlocks "
        "  FROM latest_metrics "
        "  GROUP BY cluster_id"
        "), "
        "lock_count AS ("
        "  SELECT cluster_id, COUNT(*) AS blocking_count "
        "  FROM blocking_locks "
        "  WHERE snapshot_time > NOW() - INTERVAL '15 minutes' "
        "  GROUP BY cluster_id"
        ") "
        "SELECT "
        "  m.cluster_id, m.engine, m.engine_version, m.status, m.storage_size_gb, "
        "  a.cpu, a.aas, a.conn_active, a.conn_idle, a.storage_bytes, a.deadlocks, "
        "  COALESCE(l.blocking_count, 0) AS blocking_count "
        "FROM cluster_meta m "
        "LEFT JOIN agg a USING (cluster_id) "
        "LEFT JOIN lock_count l USING (cluster_id) "
        "ORDER BY m.cluster_id"
    )
    return {"clusters": rows}


def _audit_log(query, cluster_id, days, action_type):
    conditions = ["cluster_id = :cid", "created_at > NOW() - (:days || ' days')::interval"]
    params = {"cid": cluster_id, "days": str(days)}
    if action_type:
        conditions.append("action_type = :at")
        params["at"] = action_type
    rows = query(
        "SELECT id, action_type, tool_name, requested_by, approved_by, "
        "       LEFT(sql_text, 500) AS sql_text, status, created_at, resolved_at "
        "FROM audit_log WHERE " + " AND ".join(conditions) +
        " ORDER BY created_at DESC LIMIT 100",
        params,
    )
    return {"cluster_id": cluster_id, "days": days, "audit_entries": rows}


def _anomalies(query, cluster_id, hours, threshold):
    """Seasonal anomaly detection.

    For each metric we have a per-hour-of-week baseline (median + IQR) in
    `metric_baselines`. Robust z-score = (recent_max - median) / IQR
    (1.349×IQR ≈ 1 stddev for a normal distribution, but the IQR doesn't
    blow up on outliers, so the score is stable on a cluster that has a
    handful of legitimate spikes per day).

    Falls back to the legacy flat-mean+stddev baseline when no seasonal
    baseline exists for the current bucket (cold-start: less than ~14 days
    of history). The fallback rows are tagged `mode='flat'` so the UI can
    explain why a finding's confidence is lower."""
    rows = query(
        "WITH "
        "current_hour AS ( "
        "  SELECT (EXTRACT(DOW FROM NOW())::int * 24 + EXTRACT(HOUR FROM NOW())::int) AS how "
        "), "
        "recent AS ( "
        "  SELECT metric_type, MAX(value) AS recent_max, AVG(value) AS recent_avg "
        "  FROM metric_snapshots "
        "  WHERE cluster_id = :cid "
        "    AND ts > NOW() - (:hours || ' hours')::interval "
        "    AND (dimensions IS NULL OR dimensions::text = '{}') "
        "  GROUP BY metric_type "
        "), "
        "seasonal AS ( "
        "  SELECT b.metric_type, b.median, b.iqr, b.sample_count "
        "  FROM metric_baselines b, current_hour c "
        "  WHERE b.cluster_id = :cid AND b.hour_of_week = c.how "
        "), "
        "flat AS ( "
        "  SELECT metric_type, AVG(value) AS mean, STDDEV(value) AS stddev "
        "  FROM metric_snapshots "
        "  WHERE cluster_id = :cid "
        "    AND ts BETWEEN NOW() - INTERVAL '7 days' AND NOW() - (:hours || ' hours')::interval "
        "    AND (dimensions IS NULL OR dimensions::text = '{}') "
        "  GROUP BY metric_type "
        "  HAVING STDDEV(value) > 0 AND COUNT(*) > 50 "
        ") "
        "SELECT "
        "  r.metric_type, "
        "  r.recent_max, "
        "  r.recent_avg, "
        "  COALESCE(s.median, f.mean) AS baseline_mean, "
        "  COALESCE(s.iqr, f.stddev) AS baseline_stddev, "
        "  CASE WHEN s.iqr IS NOT NULL "
        "    THEN (r.recent_max - s.median) / NULLIF(s.iqr, 0) "
        "    ELSE (r.recent_max - f.mean) / NULLIF(f.stddev, 0) "
        "  END AS z_score, "
        "  CASE WHEN s.iqr IS NOT NULL THEN 'seasonal' ELSE 'flat' END AS mode, "
        "  s.sample_count "
        "FROM recent r "
        "LEFT JOIN seasonal s ON s.metric_type = r.metric_type "
        "LEFT JOIN flat     f ON f.metric_type = r.metric_type "
        "WHERE (s.iqr IS NOT NULL OR f.stddev IS NOT NULL) "
        "  AND ABS( "
        "    CASE WHEN s.iqr IS NOT NULL "
        "      THEN (r.recent_max - s.median) / NULLIF(s.iqr, 0) "
        "      ELSE (r.recent_max - f.mean) / NULLIF(f.stddev, 0) "
        "    END "
        "  ) >= :threshold "
        "ORDER BY 6 DESC "  # ABS of z_score column
        "LIMIT 20",
        {"cid": cluster_id, "hours": str(hours), "threshold": float(threshold)},
    )
    return {"cluster_id": cluster_id, "hours": hours, "threshold": threshold, "anomalies": rows}


def _schema_changes(query, cluster_id, days):
    rows = query(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, n_live_tup, snapshot_time "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        "), "
        "baseline AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, n_live_tup, snapshot_time "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid "
        "  AND snapshot_time < NOW() - (:days || ' days')::interval "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        ") "
        "SELECT "
        "  COALESCE(l.schema_name, b.schema_name) AS schema_name, "
        "  COALESCE(l.table_name, b.table_name) AS table_name, "
        "  b.n_live_tup AS baseline_rows, "
        "  l.n_live_tup AS current_rows, "
        "  CASE "
        "    WHEN b.table_name IS NULL THEN 'created' "
        "    WHEN l.table_name IS NULL THEN 'dropped' "
        "    ELSE 'changed' "
        "  END AS change_type, "
        "  b.snapshot_time AS baseline_time, "
        "  l.snapshot_time AS current_time "
        "FROM latest l "
        "FULL OUTER JOIN baseline b "
        "  ON l.schema_name = b.schema_name AND l.table_name = b.table_name "
        "WHERE b.table_name IS NULL "
        "   OR l.table_name IS NULL "
        "   OR (b.n_live_tup IS NOT NULL AND l.n_live_tup IS NOT NULL "
        "       AND ABS(l.n_live_tup - b.n_live_tup) > GREATEST(b.n_live_tup * 0.5, 1000)) "
        "ORDER BY change_type, schema_name, table_name "
        "LIMIT 50",
        {"cid": cluster_id, "days": str(days)},
    )
    return {"cluster_id": cluster_id, "days": days, "changes": rows}


def _blocking_locks(query, cluster_id):
    rows = query(
        "SELECT snapshot_time, blocked_pid, blocked_user, blocking_pid, blocking_user, "
        "  blocked_query, blocking_query, locktype, blocked_mode, blocking_mode, "
        "  relation, blocked_duration_sec "
        "FROM blocking_locks "
        "WHERE cluster_id = :cid "
        "AND snapshot_time > NOW() - INTERVAL '15 minutes' "
        "ORDER BY snapshot_time DESC, blocked_duration_sec DESC LIMIT 30",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "locks": rows}


def _cluster_settings(query, cluster_id):
    rows = query(
        "SELECT name, value, unit, updated_at FROM cluster_settings "
        "WHERE cluster_id = :cid ORDER BY name",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "settings": rows}


def _long_running(query, cluster_id):
    rows = query(
        "SELECT pid, username, state, duration_sec, xact_duration_sec, "
        "  query_text, wait_event_type, wait_event, client_addr, snapshot_time "
        "FROM long_running_queries "
        "WHERE cluster_id = :cid "
        "AND snapshot_time > NOW() - INTERVAL '15 minutes' "
        "ORDER BY snapshot_time DESC, duration_sec DESC "
        "LIMIT 30",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "queries": rows}


def _table_sizes(query, cluster_id):
    rows = query(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, n_live_tup, total_bytes, table_bytes, index_bytes, snapshot_time "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        ") "
        "SELECT schema_name, table_name, n_live_tup, total_bytes, table_bytes, index_bytes, "
        "  CASE WHEN total_bytes > 0 THEN index_bytes::float / total_bytes ELSE 0 END AS index_ratio "
        "FROM latest "
        "WHERE total_bytes IS NOT NULL "
        "ORDER BY total_bytes DESC NULLS LAST "
        "LIMIT 30",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "tables": rows}


# DBOps' recommended extensions. Mirrors the static list in
# pg_health_checks.RECOMMENDED_EXTENSIONS so frontend can render a single
# matrix (installed vs recommended). Keep these two lists in sync.
_RECOMMENDED_EXTENSIONS = [
    {"extname": "pg_stat_statements", "severity": "warning",
     "why": "Per-query latency aggregates feed slow-query panels + AI insight."},
    {"extname": "auto_explain", "severity": "info",
     "why": "Auto-captures EXPLAIN for slow queries — invaluable for post-mortem."},
    {"extname": "pgstattuple", "severity": "warning",
     "why": "Precise bloat measurement instead of the size-based estimate."},
    {"extname": "pg_repack", "severity": "info",
     "why": "VACUUM FULL alternative that doesn't take an exclusive lock."},
    {"extname": "pg_hint_plan", "severity": "info",
     "why": "Override planner choices when stats mislead it."},
    {"extname": "pg_cron", "severity": "info",
     "why": "Schedule VACUUM/ANALYZE jobs without an external scheduler."},
]


def _extensions(query, cluster_id):
    """Return the installed extensions list for a cluster plus a recommended-
    extensions matrix with per-row install status. UI shows them side-by-side."""
    installed = query(
        "SELECT extname, extversion, updated_at "
        "FROM cluster_extensions WHERE cluster_id = :cid "
        "ORDER BY extname",
        {"cid": cluster_id},
    )
    installed_names = {r["extname"] for r in installed}
    recommended = [
        {**rec, "installed": rec["extname"] in installed_names}
        for rec in _RECOMMENDED_EXTENSIONS
    ]
    return {
        "cluster_id": cluster_id,
        "installed": installed,
        "recommended": recommended,
    }


def _health_findings(query, cluster_id):
    """Return the *latest* snapshot of maintenance health findings for this
    cluster. Older snapshots stay in the table for trend analysis but the
    dashboard panel only ever shows the most recent one."""
    rows = query(
        "WITH latest AS ("
        "  SELECT MAX(snapshot_time) AS ts FROM cluster_health_findings WHERE cluster_id = :cid"
        ") "
        "SELECT id, check_type, severity, subject, value_str, threshold_str, recommendation, details, snapshot_time "
        "FROM cluster_health_findings, latest "
        "WHERE cluster_id = :cid AND snapshot_time = latest.ts "
        "ORDER BY "
        "  CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
        "  check_type, subject",
        {"cid": cluster_id},
    )
    counts = {"critical": 0, "warning": 0, "info": 0}
    for r in rows:
        sev = r.get("severity", "info")
        if sev in counts:
            counts[sev] += 1
    snapshot_time = rows[0]["snapshot_time"] if rows else None
    return {
        "cluster_id": cluster_id,
        "snapshot_time": snapshot_time,
        "counts": counts,
        "findings": rows,
    }


def _vacuum_stats(query, cluster_id):
    rows = query(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, n_live_tup, n_dead_tup, "
        "    seq_scan, idx_scan, last_vacuum, last_analyze "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        ") "
        "SELECT schema_name, table_name, n_live_tup, n_dead_tup, "
        "  CASE WHEN (n_live_tup + n_dead_tup) > 0 "
        "    THEN n_dead_tup::float / (n_live_tup + n_dead_tup) "
        "    ELSE 0 END AS bloat_ratio, "
        "  seq_scan, idx_scan, last_vacuum, last_analyze "
        "FROM latest "
        "ORDER BY bloat_ratio DESC, n_dead_tup DESC "
        "LIMIT 30",
        {"cid": cluster_id},
    )
    return {"cluster_id": cluster_id, "tables": rows}


def _index_recommendations(query, cluster_id, min_seq_ratio):
    rows = query(
        "WITH latest AS ("
        "  SELECT DISTINCT ON (schema_name, table_name) "
        "    schema_name, table_name, seq_scan, idx_scan, seq_tup_read, n_live_tup "
        "  FROM table_stats "
        "  WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
        "  ORDER BY schema_name, table_name, snapshot_time DESC"
        ") "
        "SELECT schema_name, table_name, seq_scan, idx_scan, seq_tup_read, n_live_tup, "
        "  CASE WHEN (seq_scan + idx_scan) > 0 "
        "    THEN seq_scan::float / (seq_scan + idx_scan) "
        "    ELSE 0 END AS seq_scan_ratio "
        "FROM latest "
        "WHERE seq_scan > 100 AND n_live_tup > 1000 "
        "  AND CASE WHEN (seq_scan + idx_scan) > 0 "
        "    THEN seq_scan::float / (seq_scan + idx_scan) "
        "    ELSE 0 END >= :min_ratio "
        "ORDER BY seq_tup_read DESC "
        "LIMIT 20",
        {"cid": cluster_id, "min_ratio": float(min_seq_ratio)},
    )
    return {"cluster_id": cluster_id, "min_seq_scan_ratio": min_seq_ratio, "candidates": rows}


def _wait_events(query, cluster_id, hours):
    # Performance Insights emits one "total AAS" row per snapshot with no
    # dimensions (the bucket that aggregates everything). Keeping it here
    # would double-count and shows up as a noisy "unknown / unknown" row.
    # We filter it out and derive wait_type from the event name prefix
    # (`IO:DataFileRead` → `IO`) when PI didn't send a type explicitly.
    rows = query(
        "SELECT "
        "  dimensions->>'db.wait_event.name' AS wait_event, "
        "  COALESCE( "
        "    NULLIF(dimensions->>'db.wait_event.type', ''), "
        "    CASE "
        "      WHEN dimensions->>'db.wait_event.name' = 'CPU' THEN 'CPU' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'IO:%' THEN 'IO' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'Lock:%' THEN 'Lock' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'LWLock:%' THEN 'LWLock' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'Client:%' THEN 'Client' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'IPC:%' THEN 'IPC' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'Timeout:%' THEN 'Timeout' "
        # MySQL Performance Insights surfaces wait events as
        # `wait/<type>/<subtype>/...` (e.g. `wait/io/file/innodb/innodb_data_file`).
        # The second segment is the type bucket.
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'wait/io/%' THEN 'IO' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'wait/lock/%' THEN 'Lock' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'wait/synch/%' THEN 'Sync' "
        "      WHEN dimensions->>'db.wait_event.name' LIKE 'wait/idle/%' THEN 'Idle' "
        "      ELSE 'Other' "
        "    END "
        "  ) AS wait_type, "
        "  AVG(value) AS avg_load, "
        "  MAX(value) AS max_load "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type = 'aas' "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND dimensions IS NOT NULL "
        "  AND dimensions ? 'db.wait_event.name' "
        "  AND dimensions->>'db.wait_event.name' <> '' "
        "GROUP BY wait_event, wait_type "
        "ORDER BY avg_load DESC",
        {"cid": cluster_id, "hours": str(hours)},
    )
    return {"cluster_id": cluster_id, "hours": hours, "wait_events": rows}


def lambda_handler(event, context):
    _set_origin(event)
    raw_path_early = event.get("rawPath") or event.get("path") or ""
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    query = _make_query(_rds_data(), cluster_arn, secret_arn, database)

    if raw_path_early.endswith("/multi-cluster/overview"):
        try:
            return _response(200, _multi_cluster_overview(query))
        except Exception:
            print(f"Multi-cluster overview error: {traceback.format_exc()}")
            return _response(500, {"error": "Internal server error"})

    path_params = event.get("pathParameters") or {}
    cluster_id = path_params.get("cluster_id")
    if not cluster_id or not CLUSTER_ID_RE.match(cluster_id):
        return _response(400, {"error": "invalid cluster_id"})

    qs = event.get("queryStringParameters") or {}
    raw_path = raw_path_early

    try:
        if raw_path.endswith("/timeseries"):
            metric_type = qs.get("metric", "aas")
            hours = _parse_int(qs.get("hours"), 1)
            return _response(200, _timeseries(query, cluster_id, metric_type, hours))
        if raw_path.endswith("/wait-events"):
            hours = _parse_int(qs.get("hours"), 1)
            return _response(200, _wait_events(query, cluster_id, hours))
        if raw_path.endswith("/slow-queries"):
            hours = _parse_int(qs.get("hours"), 1)
            threshold = _parse_float(qs.get("threshold_ms"), 100.0)
            return _response(200, _slow_queries(query, cluster_id, hours, threshold))
        if raw_path.endswith("/query-detail"):
            qh = qs.get("query_hash")
            if not qh:
                return _response(400, {"error": "query_hash required"})
            return _response(200, _query_detail(query, cluster_id, qh))
        if raw_path.endswith("/vacuum-stats"):
            return _response(200, _vacuum_stats(query, cluster_id))
        if raw_path.endswith("/table-sizes"):
            return _response(200, _table_sizes(query, cluster_id))
        if raw_path.endswith("/health-findings"):
            return _response(200, _health_findings(query, cluster_id))
        if raw_path.endswith("/extensions"):
            return _response(200, _extensions(query, cluster_id))
        if raw_path.endswith("/table-indexes"):
            schema = (qs.get("schema") or "").strip()
            table_name = (qs.get("table") or "").strip()
            result = _table_indexes(cluster_id, schema, table_name)
            status = 400 if "error" in result and result.get("error") in ("schema and table required",) else 200
            if "error" in result and status == 200:
                # cluster lookup / execution errors — surface as 502/404.
                status = 404 if "not registered" in str(result.get("error")) else 502
            return _response(status, result)
        if raw_path.endswith("/long-running"):
            return _response(200, _long_running(query, cluster_id))
        if raw_path.endswith("/blocking-locks"):
            return _response(200, _blocking_locks(query, cluster_id))
        if raw_path.endswith("/settings"):
            return _response(200, _cluster_settings(query, cluster_id))
        if raw_path.endswith("/schema-changes"):
            days = _parse_int(qs.get("days"), 7, min_v=1, max_v=90)
            return _response(200, _schema_changes(query, cluster_id, days))
        if raw_path.endswith("/anomalies"):
            hours = _parse_int(qs.get("hours"), 4)
            threshold = _parse_float(qs.get("threshold"), 2.5)
            return _response(200, _anomalies(query, cluster_id, hours, threshold))
        if raw_path.endswith("/audit-log"):
            days = _parse_int(qs.get("days"), 7, min_v=1, max_v=90)
            action_type = qs.get("action_type")
            return _response(200, _audit_log(query, cluster_id, days, action_type))
        if raw_path.endswith("/batch-timeseries"):
            metrics_csv = qs.get("metrics", "")
            metric_names = [m.strip() for m in metrics_csv.split(",") if m.strip()]
            hours = _parse_int(qs.get("hours"), 1)
            return _response(200, _batch_timeseries(query, cluster_id, metric_names, hours))
        if raw_path.endswith("/index-recommendations"):
            min_ratio = _parse_float(qs.get("min_seq_ratio"), 0.5)
            return _response(200, _index_recommendations(query, cluster_id, min_ratio))
        return _response(200, _overview(query, cluster_id))
    except Exception:
        print(f"Dashboard error: {traceback.format_exc()}")
        return _response(500, {"error": "Internal server error"})
