"""Generate per-cluster Maintenance Health findings.

A "finding" is one actionable observation a DBA should look at. Each finding
has a check_type, severity, subject, value vs threshold, and a one-line
recommendation. The dashboard renders them ranked by severity at the top of
the page. New finding types are added here — no new dashboard panel needed.

Check types covered in this collector:
  - txid_age            : age(relfrozenxid) approaching wraparound
  - dead_tuples         : dead_ratio_pct over recommended threshold
  - vacuum_overdue      : no autovacuum in N days on a non-trivial table
  - table_bloat         : overhead estimate (precise via pgstattuple if available)
  - index_unused        : sizable index never used since stats reset
  - extension_missing   : recommended extension not installed
  - setting_misconfigured : logging / autovacuum params away from recommendation
"""

import json

# Thresholds. Surface in tooltips so a DBA can audit them.
TXID_WARN = 150_000_000          # PG default freeze_age = 200M; warn at 150M
TXID_CRITICAL = 180_000_000
DEAD_RATIO_WARN_PCT = 20.0
DEAD_RATIO_CRITICAL_PCT = 40.0
VACUUM_OVERDUE_DAYS = 7
UNUSED_INDEX_MIN_BYTES = 10 * 1024 * 1024  # 10 MB — below this, churn cost is negligible
BLOAT_WARN_PCT = 20.0
BLOAT_CRITICAL_PCT = 40.0


# Extensions DBOps recommends. Each entry: (name, criticality, why).
RECOMMENDED_EXTENSIONS = [
    ("pg_stat_statements", "warning", "Per-query latency aggregates feed slow-query panels + AI insight."),
    ("auto_explain",        "info",    "Auto-captures EXPLAIN for slow queries — invaluable for post-mortem."),
    ("pgstattuple",         "warning", "Precise bloat measurement instead of the size-based estimate."),
    ("pg_repack",           "info",    "VACUUM FULL alternative that doesn't take an exclusive lock."),
    ("pg_hint_plan",        "info",    "Override planner choices when stats mislead it."),
    ("pg_cron",             "info",    "Schedule VACUUM/ANALYZE jobs without an external scheduler."),
]


# Logging parameters DBOps recommends (pgBadger-friendly). Each entry:
# (name, recommended_value, severity_if_off, why).
RECOMMENDED_SETTINGS = [
    ("log_checkpoints",                  "on",     "warning", "Required for checkpoint timing analysis."),
    ("log_connections",                  "on",     "info",    "pgBadger session report needs this."),
    ("log_disconnections",               "on",     "info",    "pgBadger session report needs this."),
    ("log_lock_waits",                   "on",     "warning", "Lock contention diagnosis depends on this."),
    ("log_autovacuum_min_duration",      "0",      "warning", "0 logs every autovacuum — pgBadger correlates with bloat."),
    ("log_min_duration_statement",       "1000",   "warning", "Below 1s queries shouldn't log; 1000ms is a reasonable floor."),
    ("log_temp_files",                   "0",      "info",    "Catches queries that spill to disk."),
]


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def _double(field):
    if field.get("isNull"):
        return 0.0
    return field.get("doubleValue") or float(field.get("longValue") or 0)


def _bool(field):
    return field.get("booleanValue", False) if not field.get("isNull") else False


def _query(rds_data, cluster_arn, secret_arn, database, sql, params=None):
    sql_params = []
    if params:
        for k, v in params.items():
            sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=database,
        sql=f"/* source=dbops-etl-health */ {sql}",
        parameters=sql_params,
        includeResultMetadata=True,
    )
    return resp


def _has_extension(records, ext_name):
    for rec in records:
        if _str(rec[0]) == ext_name:
            return True
    return False


def collect_pg_health_checks(rds_data, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    findings = []

    def add(check_type, severity, subject, value, threshold, recommendation, details=None):
        findings.append({
            "check_type": check_type,
            "severity": severity,
            "subject": subject,
            "value_str": str(value),
            "threshold_str": str(threshold),
            "recommendation": recommendation,
            "details": json.dumps(details or {}),
        })

    # --- 1. Transaction ID age per database ---
    try:
        resp = _query(rds_data, target_cluster_arn, target_secret_arn, database,
                      "SELECT datname, age(datfrozenxid) FROM pg_database WHERE datname NOT IN ('template0','template1') ORDER BY 2 DESC")
        for rec in resp.get("records", []):
            db_name = _str(rec[0])
            age_val = _long(rec[1])
            if age_val >= TXID_CRITICAL:
                add("txid_age", "critical", f"db:{db_name}", f"age={age_val:,}",
                    f"< {TXID_CRITICAL:,}",
                    "Run VACUUM FREEZE on hot tables immediately — wraparound risk imminent.",
                    {"db_name": db_name, "age": age_val})
            elif age_val >= TXID_WARN:
                add("txid_age", "warning", f"db:{db_name}", f"age={age_val:,}",
                    f"< {TXID_WARN:,}",
                    "Schedule a manual VACUUM FREEZE pass during the next maintenance window.",
                    {"db_name": db_name, "age": age_val})
    except Exception as e:
        print(f"[health] txid db check failed: {e}")

    # --- 2. Transaction ID age per table (top offenders) ---
    try:
        resp = _query(rds_data, target_cluster_arn, target_secret_arn, database,
                      "SELECT schemaname, relname, age(c.relfrozenxid) AS table_age "
                      "FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid "
                      "WHERE schemaname NOT IN ('pg_catalog','information_schema') "
                      "ORDER BY age(c.relfrozenxid) DESC LIMIT 10")
        for rec in resp.get("records", []):
            schema = _str(rec[0])
            relname = _str(rec[1])
            age_val = _long(rec[2])
            if age_val >= TXID_CRITICAL:
                add("txid_age", "critical", f"{schema}.{relname}", f"age={age_val:,}",
                    f"< {TXID_CRITICAL:,}",
                    f"VACUUM FREEZE {schema}.{relname} now — wraparound risk.",
                    {"schema": schema, "table": relname, "age": age_val})
            elif age_val >= TXID_WARN:
                add("txid_age", "warning", f"{schema}.{relname}", f"age={age_val:,}",
                    f"< {TXID_WARN:,}",
                    f"VACUUM FREEZE {schema}.{relname} during the next window.",
                    {"schema": schema, "table": relname, "age": age_val})
    except Exception as e:
        print(f"[health] txid table check failed: {e}")

    # --- 3. Dead tuple ratio ---
    try:
        resp = _query(rds_data, target_cluster_arn, target_secret_arn, database,
                      "SELECT schemaname, relname, n_live_tup, n_dead_tup, "
                      "  CASE WHEN n_live_tup > 0 THEN (n_dead_tup::float / n_live_tup * 100) ELSE 0 END AS dead_pct, "
                      "  EXTRACT(EPOCH FROM (NOW() - COALESCE(last_autovacuum, last_vacuum)))/86400 AS days_since_vacuum "
                      "FROM pg_stat_user_tables "
                      "WHERE n_dead_tup > 1000 "
                      "ORDER BY n_dead_tup DESC LIMIT 20")
        for rec in resp.get("records", []):
            schema = _str(rec[0])
            relname = _str(rec[1])
            n_live = _long(rec[2])
            n_dead = _long(rec[3])
            dead_pct = _double(rec[4])
            days = _double(rec[5]) if not rec[5].get("isNull") else None
            if dead_pct >= DEAD_RATIO_CRITICAL_PCT:
                add("dead_tuples", "critical", f"{schema}.{relname}",
                    f"{dead_pct:.1f}% ({n_dead:,} dead / {n_live:,} live)",
                    f"< {DEAD_RATIO_CRITICAL_PCT:.0f}%",
                    f"Run VACUUM (ANALYZE) {schema}.{relname}. Autovacuum is not keeping up.",
                    {"schema": schema, "table": relname, "n_dead": n_dead, "n_live": n_live, "dead_pct": dead_pct})
            elif dead_pct >= DEAD_RATIO_WARN_PCT:
                add("dead_tuples", "warning", f"{schema}.{relname}",
                    f"{dead_pct:.1f}% ({n_dead:,} dead / {n_live:,} live)",
                    f"< {DEAD_RATIO_WARN_PCT:.0f}%",
                    f"Consider VACUUM ANALYZE {schema}.{relname}.",
                    {"schema": schema, "table": relname, "n_dead": n_dead, "n_live": n_live, "dead_pct": dead_pct})
            # Vacuum overdue
            if days is not None and days > VACUUM_OVERDUE_DAYS and n_live > 1000:
                add("vacuum_overdue", "warning", f"{schema}.{relname}",
                    f"{days:.0f}d since last vacuum",
                    f"< {VACUUM_OVERDUE_DAYS}d",
                    f"No autovacuum in {days:.0f} days on a {n_live:,}-row table — check autovacuum thresholds.",
                    {"schema": schema, "table": relname, "days_since_vacuum": days})
    except Exception as e:
        print(f"[health] dead tuple check failed: {e}")

    # --- 4. Index unused (size-gated) ---
    try:
        resp = _query(rds_data, target_cluster_arn, target_secret_arn, database,
                      "SELECT s.schemaname, s.relname, s.indexrelname, "
                      "  pg_relation_size(s.indexrelid)::bigint AS bytes "
                      "FROM pg_stat_user_indexes s "
                      "JOIN pg_index ix ON ix.indexrelid = s.indexrelid "
                      "WHERE s.idx_scan = 0 AND NOT ix.indisprimary AND NOT ix.indisunique "
                      "  AND pg_relation_size(s.indexrelid) > " + str(UNUSED_INDEX_MIN_BYTES) + " "
                      "ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT 30")
        for rec in resp.get("records", []):
            schema = _str(rec[0])
            relname = _str(rec[1])
            idx_name = _str(rec[2])
            bytes_v = _long(rec[3])
            add("index_unused", "warning", f"{schema}.{relname} → {idx_name}",
                f"never used since stats reset, size {bytes_v // (1024*1024)}MB",
                "> 0 scans",
                f"Consider DROP INDEX {idx_name}. Wastes disk + DML overhead, never used in queries.",
                {"schema": schema, "table": relname, "index": idx_name, "bytes": bytes_v})
    except Exception as e:
        print(f"[health] unused index check failed: {e}")

    # --- 5. Extensions installed (one query, then derive missing) ---
    installed_extensions = []
    try:
        resp = _query(rds_data, target_cluster_arn, target_secret_arn, database,
                      "SELECT extname FROM pg_extension")
        installed_extensions = [_str(r[0]) for r in resp.get("records", [])]
    except Exception as e:
        print(f"[health] pg_extension query failed: {e}")
    for name, severity, why in RECOMMENDED_EXTENSIONS:
        if name not in installed_extensions:
            add("extension_missing", severity, name,
                "not installed", "installed",
                f"{why} Run: CREATE EXTENSION IF NOT EXISTS {name};",
                {"extension": name, "reason": why})

    # --- 6. Setting recommendations ---
    try:
        names_csv = ",".join(f"'{n}'" for n, _, _, _ in RECOMMENDED_SETTINGS)
        resp = _query(rds_data, target_cluster_arn, target_secret_arn, database,
                      f"SELECT name, setting FROM pg_settings WHERE name IN ({names_csv})")
        current = {_str(r[0]): _str(r[1]) for r in resp.get("records", [])}
        for name, recommended, severity, why in RECOMMENDED_SETTINGS:
            cur = current.get(name)
            if cur is None:
                continue
            # log_min_duration_statement: any positive int is acceptable; flag only when it's -1 (off)
            # or 0 (logs everything, too noisy).
            ok = False
            if name == "log_min_duration_statement":
                try:
                    iv = int(cur)
                    ok = iv > 0
                except ValueError:
                    ok = False
            else:
                ok = cur == recommended
            if not ok:
                add("setting_misconfigured", severity, name,
                    f"current={cur}", f"recommended={recommended}",
                    why,
                    {"setting": name, "current": cur, "recommended": recommended})
    except Exception as e:
        print(f"[health] settings check failed: {e}")

    # --- 7. Table bloat (pgstattuple if available, else size-overhead estimate) ---
    has_pgstattuple = "pgstattuple" in installed_extensions
    try:
        if has_pgstattuple:
            sql = (
                "SELECT schemaname, relname, "
                "  (pgstattuple(schemaname || '.' || relname)).dead_tuple_percent AS bloat_pct "
                "FROM pg_stat_user_tables "
                "WHERE pg_total_relation_size(schemaname || '.' || relname) > 100 * 1024 * 1024 "
                "ORDER BY pg_total_relation_size(schemaname || '.' || relname) DESC LIMIT 10"
            )
        else:
            # Rough estimate — overhead = (total - heap) / total. Indexes inflate
            # this so the number is approximate; we flag anything >= warn threshold.
            sql = (
                "SELECT schemaname, relname, "
                "  ROUND( "
                "    GREATEST(0, (pg_total_relation_size(schemaname || '.' || relname) - pg_relation_size(schemaname || '.' || relname))::numeric "
                "    / NULLIF(pg_total_relation_size(schemaname || '.' || relname), 0) * 100), 1) AS overhead_pct "
                "FROM pg_stat_user_tables "
                "WHERE pg_total_relation_size(schemaname || '.' || relname) > 100 * 1024 * 1024 "
                "ORDER BY pg_total_relation_size(schemaname || '.' || relname) DESC LIMIT 10"
            )
        resp = _query(rds_data, target_cluster_arn, target_secret_arn, database, sql)
        for rec in resp.get("records", []):
            schema = _str(rec[0])
            relname = _str(rec[1])
            pct = _double(rec[2])
            precision = "precise" if has_pgstattuple else "estimate"
            if pct >= BLOAT_CRITICAL_PCT:
                add("table_bloat", "critical", f"{schema}.{relname}",
                    f"bloat ≈ {pct:.1f}% ({precision})",
                    f"< {BLOAT_CRITICAL_PCT:.0f}%",
                    f"pg_repack {schema}.{relname} (or VACUUM FULL during maintenance window).",
                    {"schema": schema, "table": relname, "pct": pct, "precision": precision})
            elif pct >= BLOAT_WARN_PCT:
                add("table_bloat", "warning", f"{schema}.{relname}",
                    f"bloat ≈ {pct:.1f}% ({precision})",
                    f"< {BLOAT_WARN_PCT:.0f}%",
                    f"Consider pg_repack or scheduled VACUUM on {schema}.{relname}.",
                    {"schema": schema, "table": relname, "pct": pct, "precision": precision})
    except Exception as e:
        print(f"[health] bloat check failed: {e}")

    # --- Write findings as a single snapshot.
    # All rows share one snapshot_time so the dashboard's MAX(snapshot_time)
    # query returns the full set together. Without this, NOW() evaluates
    # per-row at millisecond resolution and only the last finding surfaces.
    from datetime import datetime, timezone
    snapshot_ts = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for f in findings:
        cache_execute(
            "INSERT INTO cluster_health_findings "
            "(cluster_id, snapshot_time, check_type, severity, subject, value_str, threshold_str, recommendation, details) "
            "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, :value_str, :threshold_str, :recommendation, :details::jsonb)",
            {
                "cluster_id": cluster_id,
                "ts": snapshot_ts,
                "check_type": f["check_type"],
                "severity": f["severity"],
                "subject": f["subject"],
                "value_str": f["value_str"],
                "threshold_str": f["threshold_str"],
                "recommendation": f["recommendation"],
                "details": f["details"],
            },
        )
        inserted += 1

    counts = {"critical": 0, "warning": 0, "info": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "cluster_id": cluster_id,
        "findings_inserted": inserted,
        "counts": counts,
        "pgstattuple_available": has_pgstattuple,
    }
