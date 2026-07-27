"""Seasonal baseline trainer for anomaly detection.

Treats each (cluster, metric, hour_of_week) bucket as an independent
distribution and stores median + IQR. Detection at query time compares the
current bucket's observed value against its bucket's baseline, a robust
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
  2. MIN_IQR_MEDIAN_FRACTION, capped by MAX_IQR_FLOOR_INFLATION. The absolute
     0.01 IQR floor does nothing at a median of 20, so the IQR floor is also 5%
     of |median|: at median 20.1 the spread floor is 1.005, the 0.9 move above
     scores z=0.9 instead of 9.0, and a metric whose real spread is at or above
     0.5% of its median needs a >= 10% move to reach the default threshold of
     2.0 (measured at median 20.1: real IQR 0.1005 and real IQR 1.0 both floor
     at 1.005 and both hit z=2.0 on a 2.01 move, 10.00% of the median). Below
     0.5% the cap lowers the floor, so a tighter bucket flags on LESS, not more:
     real IQR 0.04 (0.199% of the median) floors at 0.400 and reaches z=2.0 on
     a 0.80 move, 3.98% of the median.
     The floor may inflate the OBSERVED P75-P25 by at most 10x. That cap does
     NOT move the survival boundary: a bucket keeps its real IQR exactly when
     that IQR is >= 5% of |median|, unchanged from the uncapped version, because
     once the observed spread clears 0.5% of |median| the LEAST resolves to the
     flat 5% of |median| again. 0.5% is only where the CAP stops binding, not
     where the real IQR starts surviving. Swept on PostgreSQL 14.18 through this
     trainer's own SQL, median 99.50, 12 samples:

       real IQR 0.1250 (0.126%) -> trained 1.2500  (cap binds: 10x observed)
       real IQR 0.4975 (0.500%) -> trained 4.9750  (cap stops binding here)
       real IQR 0.6000 (0.603%) -> trained 4.9750  (real IQR still replaced)
       real IQR 4.9000 (4.925%) -> trained 4.9750  (real IQR still replaced)
       real IQR 4.9750 (5.000%) -> trained 4.9750  (survives, the boundary)
       real IQR 5.5000 (5.528%) -> trained 5.5000  (survives)

     The median-20.1 bucket above is the same story: its real IQR of 0.2 is
     0.995% of its median, well over 0.5%, and it is stored as 1.005, not 0.2.
     What the cap DOES buy is the high-median tight-spread class the unbounded
     floor silenced outright, measured on the same PostgreSQL: a healthy
     BufferCacheHitRatio bucket (12 samples, median 99.50, real IQR 0.125)
     floored at 4.975, so a collapse to 95% scored z=-0.905 and was invisible to
     BOTH the agent (threshold 2.0) and the dashboard (2.5) while
     docdb_findings.CACHE_HIT_WARNING_PCT calls 95% a warning. Capped, the same
     bucket floors at 1.250 and 95% scores z=-3.600 (90% scores -7.600).

KNOWN CEILING (how narrow the cap's rescue really is). The cap rescues the
tight-spread end of the high-median class only. Take the metric the review was
filed on, BufferCacheHitRatio at median 99.50 collapsing to 95.0, a drop of 4.5,
at the agent's default threshold of 2.0: flagging needs a trained IQR <= 2.25,
and the trained IQR is 10x the observed spread only while that spread is under
0.4975, so the rescued band is a real IQR up to 0.225, about 0.2% of |median|.
Between roughly 0.2% and the 5% survival boundary, the relative floor still
mutes a collapse of that same magnitude. Swept on PostgreSQL 14.18 through this
trainer's SQL and the shipped detect_anomalies scoring, median 99.50 -> 95.0:

  real IQR 0.1250 (0.126%) -> trained 1.2500  z=-3.600  flagged at 2.0 and 2.5
  real IQR 0.2250 (0.226%) -> trained 2.2500  z=-2.000  flagged at 2.0 only
  real IQR 0.2260 (0.227%) -> trained 2.2600  z=-1.991  silent at both
  real IQR 0.4000 (0.402%) -> trained 4.0000  z=-1.125  silent at both
  real IQR 3.0000 (3.015%) -> trained 4.9750  z=-0.905  silent at both

The dashboard's default threshold of 2.5 narrows the band further (measured: a
real IQR of 0.17 is still flagged, 0.19 is not). The reported 0.125 bucket sits
inside the band, so that case is genuinely fixed, but a healthy cache-hit hour
with somewhat more real jitter is not. Deliberately NOT widened: raising the cap
or lowering MIN_IQR_MEDIAN_FRACTION re-opens the median-20.1 false positive the
floor exists to suppress, and 10x is exactly what holds that bucket at its 1.005
floor (10 * its real IQR of 0.2 is 2.0, above the relative floor, so the cap is
not the binding branch there). The properly targeted fix is the same
per-metric-unit floor the ceiling below needs, and that unit table does not
exist in this repo.

KNOWN CEILING (zero-median counters). Neither guard helps a bucket that is all
zeros, which is the HEALTHY shape for a counter like deadlocks or
blocked_queries: the relative branch is 0 * 0.05 = 0, so the absolute 0.01 floor
wins and a single event scores z = (1 - 0) / 0.01 = 100 as mode='seasonal'
(measured, same PostgreSQL). The sample gate cannot help either, 12 zero samples
is one normally observed hour. Deliberately NOT fixed with a "one event"
absolute floor, because metric_type units are not uniform in this table:
`read_latency` / `write_latency` are raw CloudWatch SECONDS, so a 1.0 floor would
score a 20 ms latency spike on an idle cluster at z=0.02 and hide it forever,
which is the same silencing bug the cap above removes. The real fix is a
per-metric-unit floor, and the unit table for it does not exist yet.

Both guards MOVE baselines already trained for relational / rds_instance, which
is the point: a baseline built from one hour of one day was never seasonal. The
INSERT only upserts, so a row the tighter gate now rejects would otherwise sit
there forever, hence the DELETE of sub-floor rows before it.
"""

RECOMPUTE_INTERVAL_HOURS = 1
LOOKBACK_DAYS = 14
# See the two guards in the module docstring. 12 = one fully observed hour at
# the 5-minute ETL cadence; 0.05 = the IQR floor as a fraction of |median|, and
# 10 = the most that floor may inflate the observed P75-P25.
MIN_BUCKET_SAMPLES = 12
MIN_IQR_MEDIAN_FRACTION = 0.05
MAX_IQR_FLOOR_INFLATION = 10


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
    # `IQR = P75 - P25`, floored at 5% of |median| but never more than 10x the
    # observed spread, and at 0.01: no divide-by-zero for a metric that never
    # varies, no z=9 out of a 0.0999 spread around a median of 20, and no muting
    # of a high-median tight-spread metric like a 99.5% cache hit ratio (see the
    # two guards and the zero-median ceiling in the module docstring).
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
        "    LEAST( "
        f"      ABS(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY value)) * {MIN_IQR_MEDIAN_FRACTION}, "
        "      (PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY value) "
        f"       - PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY value)) * {MAX_IQR_FLOOR_INFLATION} "
        "    ), "
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
