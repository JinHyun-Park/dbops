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

TWO GUARDS AGAINST A CONFIDENT-LOOKING FALSE POSITIVE (both proved on real
PostgreSQL: 3 samples of 20.0 / 20.1 / 20.2 trained median=20.1, IQR=0.0999,
and a later 21.0 reading, 0.9 above the median, scored a robust z of 9.0 and
was reported as mode='seasonal'):

  1. MIN_BUCKET_SAMPLES. The ETL runs every 5 minutes (STATS_COLLECTION_
     INTERVAL_MIN), so ONE fully observed hour is 12 samples and one
     hour-of-week bucket collects 12 per week. 3 samples is "we saw three
     points once", not an observed hour, so the floor is 12: the bucket's
     whole hour was sampled at least once. Not 24 (two weekly occurrences):
     the 14-day lookback holds exactly two, so a single missed ETL cycle would
     starve that bucket permanently.
  2. MIN_IQR_MEDIAN_FRACTION. The absolute 0.01 IQR floor does nothing at a
     median of 20, so the IQR floor is also 5% of |median|: at median 20.1 the
     spread floor is 1.005, the 0.9 move above scores z=0.9 instead of 9.0, and
     a flat metric now needs a >= 10% move above its median to reach the
     default threshold of 2.0. A bucket with real spread keeps its real IQR.

Both guards MOVE baselines already trained for relational / rds_instance, which
is the point: a baseline built from one hour of one day was never seasonal. The
INSERT only upserts, so a row the tighter gate now rejects would otherwise sit
there forever, hence the DELETE of sub-floor rows before it.
"""

RECOMPUTE_INTERVAL_HOURS = 1
LOOKBACK_DAYS = 14
# See the two guards in the module docstring. 12 = one fully observed hour at
# the 5-minute ETL cadence; 0.05 = the IQR floor as a fraction of |median|.
MIN_BUCKET_SAMPLES = 12
MIN_IQR_MEDIAN_FRACTION = 0.05


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

    # Retire buckets the sample floor now rejects: the INSERT below only
    # upserts, so a thin bucket trained under the old >= 3 gate would keep
    # scoring seasonal anomalies forever.
    _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "DELETE FROM metric_baselines "
        f"WHERE cluster_id = :cid AND sample_count < {MIN_BUCKET_SAMPLES}",
        {"cid": cluster_id},
    )

    # Bucket-by-hour-of-week median + IQR over LOOKBACK_DAYS.
    # `IQR = P75 - P25`, floored at 5% of |median| and at 0.01: no divide-by-zero
    # for a metric that never varies, and no z=9 out of a 0.0999 spread around a
    # median of 20 (see the two guards in the module docstring).
    # We restrict to the "total" rows (dimensions empty): per-wait-event, per-GSI
    # and per-instance rows carry the SAME metric_type and would both explode the
    # row count and poison every baseline with fractions of the cluster total.
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
        "    - PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY value), "
        f"    ABS(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value)) * {MIN_IQR_MEDIAN_FRACTION}, "
        "    0.01 "
        "  ) AS iqr, "
        "  COUNT(*) AS sample_count, "
        "  NOW() "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        f"  AND ts > NOW() - INTERVAL '{LOOKBACK_DAYS} days' "
        "  AND ts < NOW() - INTERVAL '1 hour' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}') "
        "GROUP BY metric_type, hour_of_week "
        f"HAVING COUNT(*) >= {MIN_BUCKET_SAMPLES} "
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
