"""Cost-optimization findings — engine-agnostic.

The 17-panel dashboard already shows CPU utilization in the timeseries chart,
but a long-running DBA wants the high-level "right-sized?" question
answered at a glance without scrolling charts. We surface this as a
maintenance finding so it appears in the same ranked list as VACUUM /
bloat / extension issues.

Current rules:
  - cost_oversized              — avg 7d CPU < 30% AND p95 < 60% on a sized
                                  instance (not Serverless v2 / not burstable t-family)
                                  → recommend one-step downsize

Skipped intentionally (would need extra metadata):
  - Serverless v2 min/max ACU recommendation (requires DescribeDBClusters)
  - Reserved Instance / Savings Plan match (requires Cost Explorer)
  These are left for P3.3.2.
"""

import json
from datetime import datetime, timezone


CPU_AVG_THRESHOLD = 30.0
CPU_P95_THRESHOLD = 60.0


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
        sql=f"/* source=dbops-cost */ {sql}", parameters=sql_params,
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


def collect_cost_findings(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id):
    # Pull instance_class so we can skip Serverless/burstable.
    meta_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT instance_class FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    instance_class = (meta_rows[0]["instance_class"] if meta_rows else "") or ""

    # CPU window stats from metric_snapshots (7 days).
    cpu_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  AVG(value) AS avg_cpu, "
        "  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value) AS p95_cpu, "
        "  MAX(value) AS max_cpu, "
        "  COUNT(*) AS samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type = 'cpu' "
        "  AND ts > NOW() - INTERVAL '7 days' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')",
        {"cid": cluster_id},
    )

    if not cpu_rows or cpu_rows[0]["samples"] is None or int(cpu_rows[0]["samples"] or 0) < 20:
        return {"cluster_id": cluster_id, "skipped": "insufficient_cpu_history"}

    avg_cpu = float(cpu_rows[0]["avg_cpu"] or 0)
    p95_cpu = float(cpu_rows[0]["p95_cpu"] or 0)
    max_cpu = float(cpu_rows[0]["max_cpu"] or 0)

    # Skip families where downsize advice is meaningless / wrong.
    skip_reasons = []
    ic_lower = instance_class.lower()
    if "serverless" in ic_lower:
        skip_reasons.append("serverless (use max ACU advice instead)")
    elif ic_lower.startswith("db.t"):
        # Burstable instances are designed to spike; avg-CPU isn't a reliable
        # right-sizing signal because credits hide it.
        skip_reasons.append("burstable instance (avg CPU misleading)")

    findings_emitted = 0
    if not skip_reasons and avg_cpu < CPU_AVG_THRESHOLD and p95_cpu < CPU_P95_THRESHOLD:
        ts = datetime.now(timezone.utc).isoformat()
        # Insert via _execute with ts; cache_execute helper isn't passed in.
        _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "INSERT INTO cluster_health_findings "
            "  (cluster_id, snapshot_time, check_type, severity, subject, value_str, threshold_str, recommendation, details) "
            "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, :value_str, :threshold_str, :recommendation, :details::jsonb)",
            {
                "cluster_id": cluster_id,
                "ts": ts,
                "check_type": "cost_oversized",
                "severity": "info",
                "subject": instance_class or "instance",
                "value_str": f"avg CPU {avg_cpu:.1f}% / p95 {p95_cpu:.1f}% / max {max_cpu:.1f}%",
                "threshold_str": f"< {CPU_AVG_THRESHOLD:.0f}% avg & < {CPU_P95_THRESHOLD:.0f}% p95 → consider downsize",
                "recommendation": (
                    f"7-day avg CPU is {avg_cpu:.1f}% on {instance_class or 'this instance'} — "
                    "consider one-step smaller (typically 30-50% monthly savings). "
                    "Re-evaluate after the smaller size has 1 week of production traffic."
                ),
                "details": json.dumps({
                    "instance_class": instance_class,
                    "avg_cpu": avg_cpu,
                    "p95_cpu": p95_cpu,
                    "max_cpu": max_cpu,
                    "window_days": 7,
                }),
            },
        )
        findings_emitted = 1

    return {
        "cluster_id": cluster_id,
        "instance_class": instance_class,
        "avg_cpu": avg_cpu,
        "p95_cpu": p95_cpu,
        "skip_reasons": skip_reasons,
        "findings_emitted": findings_emitted,
    }
