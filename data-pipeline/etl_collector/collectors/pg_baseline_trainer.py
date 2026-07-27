"""Seasonal baseline trainer for anomaly detection.

Treats each (cluster, metric, hour_of_week) bucket as an independent
distribution and stores median + IQR. Detection at query time compares the
current bucket's observed value against its bucket's baseline — a robust
z-score that ignores flat means and doesn't false-positive on daily cycles.

Recomputed at most once per hour per cluster (the bucket SQL is cheap but
running it every 5-minute ETL cycle is wasteful and creates timestamp churn).

Engine-agnostic: it reads `metric_snapshots` and writes `metric_baselines`,
nothing else, so every engine family is trained by the same SQL. Callers: all
five family branches of the ETL handler.

KNOWN CEILING (thin buckets). `HAVING COUNT(*) >= 3` accepts a bucket that a
single hour of one day filled: the 5-minute ETL puts ~12 samples in one
hour-of-week bucket per WEEK, so after a day of history a bucket's median/IQR
describe the spread WITHIN one hour on one day, not the week-over-week spread
the seasonal model assumes. The IQR is then near 0 (floored at 0.01), and
`detect_anomalies` reports mode='seasonal' with a huge robust z for a small
deviation, i.e. a confident-looking false positive. This is pre-existing
behaviour for every family, unchanged here.
"""

RECOMPUTE_INTERVAL_HOURS = 1
LOOKBACK_DAYS = 14


def _execute(rds_data, cluster_arn, secret_arn, db_name, sql, params=None):
    """Execute one SQL against the cache DB and return parsed rows."""
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
        sql=f"/* source=dbops-baseline */ {sql}", parameters=sql_params,
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


def collect_pg_baselines(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id):
    # Time-gate: skip if any baseline row was updated within the last hour.
    gate = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(updated_at)))/3600 AS hours_since "
        "FROM metric_baselines WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    hours_since = None
    if gate and gate[0].get("hours_since") is not None:
        hours_since = float(gate[0]["hours_since"])
    if hours_since is not None and hours_since < RECOMPUTE_INTERVAL_HOURS:
        return {
            "cluster_id": cluster_id,
            "skipped": "fresh",
            "hours_since_last": round(hours_since, 2),
        }

    # Bucket-by-hour-of-week median + IQR over LOOKBACK_DAYS.
    # `IQR = P75 - P25`; floor at 0.01 so divide-by-zero in z-score is impossible
    # for metrics that don't vary at all (e.g. connections=0 on a sample cluster).
    # We restrict to the "total" rows (dimensions empty): per-wait-event, per-GSI
    # and per-instance rows carry the SAME metric_type and would both explode the
    # row count and poison every baseline with fractions of the cluster total.
    # ponytail: the >= 3 sample gate accepts a one-day-old thin bucket (see the
    # module docstring). Tightening it to >= 2 distinct days moves Aurora's
    # trained baselines, so it belongs in its own change, not here.
    _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "INSERT INTO metric_baselines "
        "  (cluster_id, metric_type, hour_of_week, median, iqr, sample_count, updated_at) "
        "SELECT "
        "  :cid, "
        "  metric_type, "
        "  (EXTRACT(DOW FROM ts)::int * 24 + EXTRACT(HOUR FROM ts)::int) AS hour_of_week, "
        "  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value) AS median, "
        "  GREATEST( "
        "    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY value) "
        "    - PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY value), 0.01 "
        "  ) AS iqr, "
        "  COUNT(*) AS sample_count, "
        "  NOW() "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        f"  AND ts > NOW() - INTERVAL '{LOOKBACK_DAYS} days' "
        "  AND ts < NOW() - INTERVAL '1 hour' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}') "
        "GROUP BY metric_type, hour_of_week "
        "HAVING COUNT(*) >= 3 "
        "ON CONFLICT (cluster_id, metric_type, hour_of_week) DO UPDATE SET "
        "  median = EXCLUDED.median, "
        "  iqr = EXCLUDED.iqr, "
        "  sample_count = EXCLUDED.sample_count, "
        "  updated_at = NOW()",
        {"cid": cluster_id},
    )

    # Report how many buckets we have now (helps detect cold-start cases).
    rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT COUNT(*) AS bucket_count, COUNT(DISTINCT metric_type) AS metric_count "
        "FROM metric_baselines WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    return {
        "cluster_id": cluster_id,
        "bucket_count": int(rows[0]["bucket_count"]) if rows else 0,
        "metric_count": int(rows[0]["metric_count"]) if rows else 0,
    }
