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
        "SELECT event_time as ts, event_type, severity, message "
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
    rows = query(
        "WITH baseline AS ("
        "  SELECT metric_type, AVG(value) AS mean, STDDEV(value) AS stddev "
        "  FROM metric_snapshots "
        "  WHERE cluster_id = :cid "
        "  AND ts BETWEEN NOW() - INTERVAL '7 days' AND NOW() - (:hours || ' hours')::interval "
        "  GROUP BY metric_type "
        "  HAVING STDDEV(value) > 0 AND COUNT(*) > 50"
        "), "
        "recent AS ("
        "  SELECT metric_type, MAX(value) AS recent_max, AVG(value) AS recent_avg "
        "  FROM metric_snapshots "
        "  WHERE cluster_id = :cid "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  GROUP BY metric_type"
        ") "
        "SELECT "
        "  r.metric_type, "
        "  r.recent_max, "
        "  r.recent_avg, "
        "  b.mean AS baseline_mean, "
        "  b.stddev AS baseline_stddev, "
        "  (r.recent_max - b.mean) / NULLIF(b.stddev, 0) AS z_score "
        "FROM recent r "
        "JOIN baseline b ON r.metric_type = b.metric_type "
        "WHERE ABS((r.recent_max - b.mean) / NULLIF(b.stddev, 0)) >= :threshold "
        "ORDER BY ABS((r.recent_max - b.mean) / NULLIF(b.stddev, 0)) DESC "
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
    rows = query(
        "SELECT "
        "  COALESCE(dimensions->>'db.wait_event.name', 'unknown') as wait_event, "
        "  COALESCE(dimensions->>'db.wait_event.type', 'unknown') as wait_type, "
        "  AVG(value) as avg_load, "
        "  MAX(value) as max_load "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "AND metric_type = 'aas' "
        "AND ts > NOW() - (:hours || ' hours')::interval "
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
