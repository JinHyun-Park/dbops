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


def _redundant_indexes(cluster_id: str) -> dict:
    """Find PG indexes that can likely be dropped — prefix-covered, exact
    duplicates, or unused (idx_scan = 0 and not constraint-backing).

    pganalyze ships this as the "Index Advisor / Redundant Indexes" panel.
    Same idea here: catch the easy wasted disk + write amplification before
    a DBA goes through `pg_stat_user_indexes` by hand. PG-only for v1 —
    MySQL exposes a different index shape and the planner heuristics are
    different enough that we don't share logic."""
    cluster = _lookup_cluster(cluster_id)
    if not cluster:
        return {"error": f"cluster {cluster_id!r} not registered", "candidates": []}
    cluster_arn = cluster.get("cluster_arn")
    secret_arn = cluster.get("secret_arn")
    db_name = cluster.get("db_name") or "postgres"
    engine = (cluster.get("engine") or "").lower()
    if not cluster_arn or not secret_arn:
        return {"error": "cluster registry missing cluster_arn/secret_arn", "candidates": []}
    if "mysql" in engine:
        return {
            "cluster_id": cluster_id,
            "engine": engine,
            "candidates": [],
            "info": "MySQL은 v1에서 지원하지 않습니다 — PostgreSQL 클러스터에서 사용하세요.",
        }

    # One round trip per cluster — pull every valid user index with its
    # ordered column list, size, and scan count. WITH ORDINALITY preserves
    # the column order so a (a,b) prefix can be distinguished from (b,a).
    sql = (
        "SELECT "
        "  n.nspname AS schema_name, "
        "  c.relname AS table_name, "
        "  ic.relname AS index_name, "
        "  pg_get_indexdef(i.indexrelid) AS definition, "
        "  pg_relation_size(i.indexrelid)::bigint AS bytes, "
        "  COALESCE(s.idx_scan, 0) AS idx_scan, "
        "  i.indisunique AS is_unique, "
        "  i.indisprimary AS is_primary, "
        "  (SELECT string_agg(COALESCE(a.attname, '(expr)'), ',' ORDER BY arr.ord) "
        "   FROM unnest(i.indkey) WITH ORDINALITY AS arr(col, ord) "
        "   LEFT JOIN pg_attribute a "
        "     ON a.attrelid = i.indrelid AND a.attnum = arr.col) AS columns "
        "FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_class ic ON ic.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid "
        "WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') "
        "  AND i.indisvalid "
        "ORDER BY n.nspname, c.relname, ic.relname"
    )

    rds_data = boto3.client("rds-data")
    try:
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=db_name,
            sql=f"/* source=dbops-dashboard-redundant */ {sql}",
            includeResultMetadata=True,
        )
    except Exception as e:
        return {"error": "execution_failed", "message": str(e)[:300], "candidates": []}

    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    indexes: list[dict] = []
    for rec in resp.get("records", []):
        row: dict = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
        indexes.append(row)

    # Group by (schema, table) and compute redundancy candidates. We treat:
    #   - "prefix"   — this index's columns are a strict prefix of another's
    #   - "duplicate"— same columns as another index (keep the larger; the
    #                  smaller is usually a leftover migration artifact)
    #   - "unused"   — idx_scan = 0 and not backing a unique/PK constraint
    # An index can only show up once — we prefer prefix > duplicate > unused
    # so the DBA sees the most explainable reason first.
    findings: list[dict] = []
    by_table: dict[tuple[str, str], list[dict]] = {}
    for idx in indexes:
        key = (idx.get("schema_name") or "", idx.get("table_name") or "")
        by_table.setdefault(key, []).append(idx)

    for (schema, tbl), group in by_table.items():
        for a in group:
            if a.get("is_primary"):
                continue  # primary key is sacred even if unused
            a_cols = (a.get("columns") or "").split(",")
            a_name = a.get("index_name") or ""
            reason = None
            covered_by = None
            for b in group:
                if b is a:
                    continue
                b_cols = (b.get("columns") or "").split(",")
                b_name = b.get("index_name") or ""
                if a_cols == b_cols:
                    # Duplicate — keep whichever is larger / has more scans
                    a_size = int(a.get("bytes") or 0)
                    b_size = int(b.get("bytes") or 0)
                    if (a_size, int(a.get("idx_scan") or 0)) < (
                        b_size,
                        int(b.get("idx_scan") or 0),
                    ):
                        reason = "duplicate"
                        covered_by = b_name
                        break
                elif (
                    len(a_cols) < len(b_cols)
                    and a_cols == b_cols[: len(a_cols)]
                    and not a.get("is_unique")
                ):
                    # Strict prefix — b covers every query a covers, plus
                    # more. Unique-index prefixes are NOT redundant (they
                    # enforce a separate uniqueness constraint).
                    reason = "prefix"
                    covered_by = b_name
                    break

            if reason is None and int(a.get("idx_scan") or 0) == 0:
                # Unused — only flag if it's not enforcing a constraint.
                if not a.get("is_unique"):
                    reason = "unused"

            if reason:
                findings.append(
                    {
                        "schema": schema,
                        "table": tbl,
                        "index_name": a_name,
                        "kind": reason,
                        "bytes": int(a.get("bytes") or 0),
                        "idx_scan": int(a.get("idx_scan") or 0),
                        "is_unique": bool(a.get("is_unique")),
                        "columns": a.get("columns") or "",
                        "definition": a.get("definition") or "",
                        "covered_by": covered_by,
                    }
                )

    findings.sort(key=lambda f: f["bytes"], reverse=True)
    total_bytes = sum(f["bytes"] for f in findings)
    return {
        "cluster_id": cluster_id,
        "engine": engine,
        "indexes_scanned": len(indexes),
        "candidates_count": len(findings),
        "total_bytes_reclaimable": total_bytes,
        "candidates": findings,
    }


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


def _timeseries(query, cluster_id, metric_type, hours, from_iso=None, to_iso=None):
    """Single-metric timeseries. Same window precedence as _batch_timeseries —
    absolute (from/to) overrides relative (hours)."""
    if from_iso and to_iso:
        rows = query(
            "SELECT ts, value, dimensions::text as dimensions "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid "
            "AND metric_type = :mt "
            "AND ts >= :from_ts::timestamptz "
            "AND ts <= :to_ts::timestamptz "
            "ORDER BY ts ASC",
            {"cid": cluster_id, "mt": metric_type, "from_ts": from_iso, "to_ts": to_iso},
        )
    else:
        rows = query(
            "SELECT ts, value, dimensions::text as dimensions "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid "
            "AND metric_type = :mt "
            "AND ts > NOW() - (:hours || ' hours')::interval "
            "ORDER BY ts ASC",
            {"cid": cluster_id, "mt": metric_type, "hours": str(hours)},
        )
    return {
        "cluster_id": cluster_id,
        "metric_type": metric_type,
        "hours": hours,
        "from": from_iso,
        "to": to_iso,
        "points": rows,
    }


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


def _batch_timeseries(
    query,
    cluster_id,
    metric_names,
    hours,
    offset_hours=0,
    from_iso=None,
    to_iso=None,
):
    """Returns metric series within the requested time window.

    Window selection precedence:
      1. If both `from_iso` and `to_iso` are valid TIMESTAMPTZ strings →
         use [from_iso, to_iso] as an *absolute* window. This is what the
         Dashboard custom time picker emits.
      2. Otherwise → use the legacy relative window
         (NOW - hours, NOW - offset_hours].

    The absolute path makes the result deterministic across requests (a
    URL with from/to can be shared), while the relative path keeps every
    legacy caller working unchanged."""
    metric_names = [m for m in metric_names if METRIC_NAME_RE.match(m)][:20]
    base_meta = {
        "cluster_id": cluster_id,
        "hours": hours,
        "offset_hours": offset_hours,
        "from": from_iso,
        "to": to_iso,
    }
    if not metric_names:
        return {**base_meta, "series": {}}

    placeholders = ", ".join(f":m{i}" for i in range(len(metric_names)))
    params = {"cid": cluster_id}
    for i, m in enumerate(metric_names):
        params[f"m{i}"] = m

    use_absolute = bool(from_iso) and bool(to_iso)
    if use_absolute:
        params["from_ts"] = from_iso
        params["to_ts"] = to_iso
        sql = (
            f"SELECT ts, metric_type, value, dimensions::text as dimensions "
            f"FROM metric_snapshots "
            f"WHERE cluster_id = :cid "
            f"AND metric_type IN ({placeholders}) "
            f"AND ts >= :from_ts::timestamptz "
            f"AND ts <= :to_ts::timestamptz "
            f"ORDER BY ts ASC"
        )
    else:
        params["hours"] = str(hours)
        params["offset"] = str(offset_hours)
        # Window: (NOW - hours, NOW - offset_hours]. When offset_hours=0 the
        # upper bound collapses to NOW, matching the original "last N hours"
        # semantics.
        sql = (
            f"SELECT ts, metric_type, value, dimensions::text as dimensions "
            f"FROM metric_snapshots "
            f"WHERE cluster_id = :cid "
            f"AND metric_type IN ({placeholders}) "
            f"AND ts > NOW() - (:hours || ' hours')::interval "
            f"AND ts <= NOW() - (:offset || ' hours')::interval "
            f"ORDER BY ts ASC"
        )

    rows = query(sql, params)

    series = {m: [] for m in metric_names}
    for r in rows:
        mt = r.get("metric_type")
        if mt in series:
            series[mt].append({"ts": r["ts"], "value": r["value"], "dimensions": r.get("dimensions")})
    return {**base_meta, "series": series}


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


# Capacity forecasting: simple linear regression on the last N days of
# metric_snapshots. The Performance MCP server has a `forecast_capacity`
# tool already, but invoking it through the agent for a dashboard panel is
# heavy + slow. We replicate the math directly against the cache DB so the
# panel renders in one round trip.
#
# Limits are deliberately conservative defaults — Aurora autoscales
# storage (cluster cap is 128 TB) so the storage value is a "well past
# any sane operator" ceiling. Connections / AAS limits come from typical
# saturation points for the popular instance classes; when cluster_settings
# carries the actual max_connections we'll override below.
_CAPACITY_METRICS = {
    # metric_type, display unit, hard cap (in stored units)
    "storage_bytes": {"limit": 128 * 1024**4, "label": "Storage"},  # 128 TiB
    "connections": {"limit": 5000, "label": "Connections"},
    "aas": {"limit": 64.0, "label": "Active Sessions"},
}


def _capacity_forecast(query, cluster_id, metric, days_lookback):
    if metric not in _CAPACITY_METRICS:
        return {"cluster_id": cluster_id, "metric": metric, "error": f"unknown metric {metric}"}
    # RDS Data API params come through as strings — we cast to interval the
    # same way the other lookback queries in this file do, instead of using
    # MAKE_INTERVAL which would need an integer-typed param. Float-cast
    # value to keep REGR_SLOPE happy when the metric is stored as integer.
    rows = query(
        "SELECT REGR_SLOPE(value::float, EXTRACT(EPOCH FROM ts) / 86400) AS slope, "
        "       (array_agg(value ORDER BY ts DESC))[1]                 AS latest, "
        "       MIN(ts)                                                 AS first_ts, "
        "       MAX(ts)                                                 AS last_ts, "
        "       COUNT(*)                                                AS samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = :mt "
        "AND ts > NOW() - (:days || ' days')::interval",
        {"cid": cluster_id, "mt": metric, "days": str(days_lookback)},
    )
    row = rows[0] if rows else {}
    slope = float(row.get("slope") or 0)
    current = float(row.get("latest") or 0)
    samples = int(row.get("samples") or 0)
    spec = _CAPACITY_METRICS[metric]
    limit = float(spec["limit"])

    # Connection limit can be looked up dynamically from cluster_settings — when
    # the cluster has a max_connections row, we trust that over our default
    # ceiling.
    if metric == "connections":
        cfg = query(
            "SELECT value FROM cluster_settings "
            "WHERE cluster_id = :cid AND name = 'max_connections' "
            "ORDER BY updated_at DESC LIMIT 1",
            {"cid": cluster_id},
        )
        try:
            mc = int(cfg[0]["value"]) if cfg else 0
            if mc > 0:
                limit = float(mc)
        except (ValueError, KeyError, TypeError):
            pass

    days_until = None
    if slope > 0 and current < limit:
        days_until = max(0, int((limit - current) / slope))
    forecast = "growing" if slope > 0.01 else "shrinking" if slope < -0.01 else "stable"

    return {
        "cluster_id": cluster_id,
        "metric": metric,
        "label": spec["label"],
        "current": current,
        "slope_per_day": slope,
        "limit": limit,
        "days_until_limit": days_until,
        "forecast": forecast,
        "samples": samples,
        "days_lookback": days_lookback,
        "projections": {
            "d30": current + slope * 30,
            "d60": current + slope * 60,
            "d90": current + slope * 90,
        },
    }


# PG log filter patterns per category. The model that drives the AI panel
# can already query CloudWatch Logs through the search_logs MCP tool, but a
# pre-categorized dashboard panel is what DBAs actually scan — pganalyze /
# Datadog DBM ship the same shape (Log Insights / Database Logs).
_LOG_CATEGORY_FILTERS = {
    "slow": "filter @message like /duration: [0-9.]+ ms/",
    "vacuum": (
        "filter @message like /automatic vacuum/ or @message like "
        "/automatic analyze/"
    ),
    "error": (
        "filter @message like /ERROR:/ or @message like /FATAL:/ or "
        "@message like /PANIC:/"
    ),
    "connection": (
        "filter @message like /connection received/ or @message like "
        "/connection authorized/ or @message like /disconnection:/"
    ),
}


def _log_insights(cluster_id, hours, category):
    """Run a CloudWatch Logs Insights query for one category of PG logs.

    Returns the most recent matching entries (raw @timestamp + @message) so
    the frontend can render them as a feed. We deliberately do NOT pre-
    aggregate into time-buckets here — DBAs reach for log insights when
    they want to see the actual line, not a count. Tight default cap (100
    entries) keeps CW Insights scan cost predictable."""
    import time

    log_group = f"/aws/rds/cluster/{cluster_id}/postgresql"
    client = boto3.client("logs")

    if category not in _LOG_CATEGORY_FILTERS and category != "all":
        category = "all"

    if category == "all":
        query_string = (
            "fields @timestamp, @message | sort @timestamp desc | limit 100"
        )
    else:
        query_string = (
            f"fields @timestamp, @message | {_LOG_CATEGORY_FILTERS[category]} "
            f"| sort @timestamp desc | limit 100"
        )

    base_result = {
        "cluster_id": cluster_id,
        "category": category,
        "hours": hours,
        "log_group": log_group,
        "entries": [],
        "count": 0,
    }

    try:
        resp = client.start_query(
            logGroupName=log_group,
            startTime=int((time.time() - hours * 3600) * 1000),
            endTime=int(time.time() * 1000),
            # CloudWatch Logs Insights does not accept SQL-style comments;
            # the source-tagging convention applies only to SQL queries.
            queryString=query_string,
        )
    except client.exceptions.ResourceNotFoundException:
        return {
            **base_result,
            "error": (
                f"Log group {log_group} not found — enable PostgreSQL log "
                "exports on the cluster (parameter group + Modify cluster → "
                "Logs)."
            ),
        }
    except Exception as e:
        return {**base_result, "error": str(e)}

    qid = resp["queryId"]
    for _ in range(25):  # ~25s budget — Lambda timeout is 30s
        r = client.get_query_results(queryId=qid)
        status = r.get("status")
        if status == "Complete":
            rows = r.get("results", []) or []
            entries = []
            for row in rows:
                fields = {f["field"]: f["value"] for f in row}
                entries.append(
                    {
                        "ts": fields.get("@timestamp"),
                        "message": fields.get("@message", ""),
                    }
                )
            return {
                **base_result,
                "entries": entries,
                "count": len(entries),
            }
        if status in ("Failed", "Cancelled"):
            return {**base_result, "error": f"query {status.lower()}"}
        time.sleep(1)

    return {**base_result, "error": "query timed out — try a smaller hours window"}


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

    # Absolute window (Dashboard custom time picker). When both are present
    # and parseable, every endpoint that supports an absolute window will use
    # them in preference to the relative `hours` arg. We accept ISO-8601
    # strings (e.g. "2026-05-18T14:00:00Z") — RDS Data API's timestamptz cast
    # tolerates either Z-suffix or "+00:00".
    from_iso = (qs.get("from") or "").strip() or None
    to_iso = (qs.get("to") or "").strip() or None

    try:
        if raw_path.endswith("/timeseries"):
            metric_type = qs.get("metric", "aas")
            hours = _parse_int(qs.get("hours"), 1)
            return _response(
                200,
                _timeseries(query, cluster_id, metric_type, hours, from_iso, to_iso),
            )
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
            offset_hours = _parse_int(qs.get("offset_hours"), 0, min_v=0)
            return _response(
                200,
                _batch_timeseries(
                    query,
                    cluster_id,
                    metric_names,
                    hours,
                    offset_hours,
                    from_iso,
                    to_iso,
                ),
            )
        if raw_path.endswith("/index-recommendations"):
            min_ratio = _parse_float(qs.get("min_seq_ratio"), 0.5)
            return _response(200, _index_recommendations(query, cluster_id, min_ratio))
        if raw_path.endswith("/log-insights"):
            hours = _parse_int(qs.get("hours"), 1, min_v=1, max_v=24)
            category = (qs.get("category") or "all").strip()
            return _response(200, _log_insights(cluster_id, hours, category))
        if raw_path.endswith("/capacity-forecast"):
            metric = (qs.get("metric") or "storage_bytes").strip()
            days_lookback = _parse_int(qs.get("days_lookback"), 30, min_v=7, max_v=90)
            return _response(
                200, _capacity_forecast(query, cluster_id, metric, days_lookback)
            )
        if raw_path.endswith("/redundant-indexes"):
            return _response(200, _redundant_indexes(cluster_id))
        return _response(200, _overview(query, cluster_id))
    except Exception:
        print(f"Dashboard error: {traceback.format_exc()}")
        return _response(500, {"error": "Internal server error"})
