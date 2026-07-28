"""detect_anomalies — seasonal, robust metric anomaly detection.

Replaces the old flat 7-day mean/stddev z-score (which ignored daily/weekly
seasonality and blew up on a cluster with a few legitimate spikes per day) with
the SAME robust seasonal baseline the dashboard already uses: a per-hour-of-week
median + IQR trained into ``metric_baselines`` by the pg_baseline_trainer
collector. Robust z = (recent_max - median) / IQR, the IQR doesn't inflate on
outliers, so the score is stable.

When no seasonal baseline exists yet for the current hour-of-week bucket
(cold-start: < ~2 weeks of history) we fall back to the legacy flat mean/stddev
and tag the row ``mode='flat'`` so the caller knows the finding is lower
confidence. Each row carries its baseline + sample_count so the agent can
explain WHY something is anomalous.

An empty ``anomalies`` list has FOUR meanings and ``baseline_mode`` is what
tells them apart. Getting this wrong told a DBA to wait two weeks while the
collector was dead, so the states are spelled out in detect_anomalies_impl.
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.metric_filters import CLUSTER_LEVEL_ONLY

# Robust seasonal baseline (median/IQR per hour-of-week) with a flat
# mean/stddev fallback. Mirrors the dashboard's seasonal anomaly query so the
# chat agent and the dashboard never disagree on what's anomalous.
#
# Deliberately UNLIMITED. One row per cluster-level metric_type, verified on
# PostgreSQL 14.18: 7 metric_types with 168 trained hour_of_week buckets each, a
# flat baseline, and 30 dimensioned rows each, returns exactly 7 rows (the
# seasonal CTE pins hour_of_week to the current bucket, and the strict dimension
# filter keeps the dimensioned rows out of `recent`).
#
# LIMIT 50 was therefore never reachable on today's collectors. Counted off the
# shipped collector tables, the deepest family is about 30 cluster-level
# metric_types (Aurora PG: 9 cluster CloudWatch + 12 Performance Insights + 5
# pg_activity connection states + 4 pg_stat_database/bgwriter); the others run 6
# (DynamoDB on-demand) to 23 (DocumentDB). 81 distinct metric_type literals exist
# across the whole data-pipeline tree, but a cluster only ever runs ONE family
# branch, so that number is not a per-cluster ceiling.
#
# The LIMIT is gone anyway because `total_checked` and the seasonal/flat
# classification have to come from the FULL scored set, not from whatever slice
# the query happened to keep: one new per-object collector would push a cluster
# past 50 and the count would silently cap and the only seasonal baseline outside
# the top-N by |z| would disappear. The display cap is applied in Python, to the
# ROWS RETURNED to the caller.
_ANOMALY_SQL = """
SELECT * FROM (
    WITH current_hour AS (
        SELECT (EXTRACT(DOW FROM NOW())::int * 24 + EXTRACT(HOUR FROM NOW())::int) AS how
    ),
    recent AS (
        SELECT metric_type, MAX(value) AS recent_max, AVG(value) AS recent_avg
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id
          AND ts > NOW() - (:hours || ' hours')::interval
          AND (dimensions IS NULL OR dimensions::text = '{}')
        GROUP BY metric_type
    ),
    seasonal AS (
        SELECT b.metric_type, b.median, b.iqr, b.sample_count
        FROM metric_baselines b, current_hour c
        WHERE b.cluster_id = :cluster_id AND b.hour_of_week = c.how
    ),
    flat AS (
        SELECT metric_type, AVG(value) AS mean, STDDEV(value) AS stddev
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id
          AND ts BETWEEN NOW() - INTERVAL '7 days' AND NOW() - (:hours || ' hours')::interval
          AND (dimensions IS NULL OR dimensions::text = '{}')
        GROUP BY metric_type
        HAVING STDDEV(value) > 0 AND COUNT(*) > 50
    )
    SELECT
        r.metric_type,
        r.recent_max,
        r.recent_avg,
        -- A seasonal row with iqr <= 0 (a metric that's constant at this hour)
        -- can't yield a robust z, so treat it like "no seasonal" and fall back
        -- to the flat baseline instead of dropping the metric entirely.
        CASE WHEN s.iqr > 0 THEN s.median ELSE f.mean END AS baseline_mean,
        CASE WHEN s.iqr > 0 THEN s.iqr ELSE f.stddev END AS baseline_stddev,
        CASE WHEN s.iqr > 0
            THEN (r.recent_max - s.median) / s.iqr
            ELSE (r.recent_max - f.mean) / NULLIF(f.stddev, 0)
        END AS z_score,
        CASE WHEN s.iqr > 0 THEN 'seasonal' ELSE 'flat' END AS mode,
        CASE WHEN s.iqr > 0 THEN s.sample_count ELSE NULL END AS sample_count
    FROM recent r
    LEFT JOIN seasonal s ON s.metric_type = r.metric_type
    LEFT JOIN flat     f ON f.metric_type = r.metric_type
    WHERE (s.iqr > 0 OR f.stddev IS NOT NULL)
) t
WHERE z_score IS NOT NULL
ORDER BY ABS(z_score) DESC
"""

# Existence probe, run ONLY when _ANOMALY_SQL scored nothing, so the normal path
# costs no extra round trip. _ANOMALY_SQL is DRIVEN from its `recent` CTE, so an
# empty result collapses two states whose operator actions are opposite: "no
# recent samples at all" (collection stopped / cluster just registered / every
# recent row is dimensioned) and "samples arrived but no baseline matched".
#
# Same :hours window as the scoring query, or the two answers could disagree.
# CLUSTER_LEVEL_ONLY, never hand-written: this reads metric_snapshots at cluster
# level, and per-instance / per-wait-event / per-GSI rows must NOT count as
# "samples exist" (they are invisible to the scoring query).
_RECENT_SAMPLES_SQL = f"""
SELECT 1
FROM metric_snapshots
WHERE cluster_id = :cluster_id
  AND ts > NOW() - (:hours || ' hours')::interval
  {CLUSTER_LEVEL_ONLY}
LIMIT 1
"""

# How many anomalies to hand back. What makes these the STRONGEST ones is the
# ORDERING, not where the cap sits: the scoring SQL already emits |z| descending,
# so the rows at or above threshold are a prefix of a sorted list and the cap can
# only ever drop the weakest. Swapping the cap and the threshold filter is an
# equivalent mutation on that input (checked: 0 tests fail when swapped), so do
# not read the placement as load-bearing. Keep in sync with the api/dashboard copy
# (parity test compares the derived output, not just the SQL).
_MAX_REPORTED = 50


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def detect_anomalies_impl(
    cache: CacheClient,
    cluster_id: str,
    hours: int = 4,
    threshold: float = 2.0,
) -> dict:
    """Score every cluster-level metric against its baseline.

    `anomalies` holds the rows at or above `threshold`, |z| descending, capped
    at `_MAX_REPORTED`. `total_checked` counts EVERY scored metric, including
    the ones below threshold and the ones past the display cap.

    `baseline_mode` says why the list can be empty, and there are four answers:

      seasonal   scored against the per-hour-of-week median + IQR baseline
      flat       no seasonal baseline for this bucket, scored against the
                 7-day mean/stddev fallback (lower confidence)
      none       recent cluster-level samples EXIST, but no baseline of either
                 kind matched, so nothing could be scored. Waiting for history
                 fixes it.
      no_samples no recent cluster-level samples at all in the window: metric
                 collection stopped, the cluster was just registered, or every
                 recent row is dimensioned. Waiting does NOT fix it.

    `none` and `no_samples` used to be one state, which reported a dead
    collector as "wait about two weeks for a baseline".
    """
    result = cache.execute(_ANOMALY_SQL, {"cluster_id": cluster_id, "hours": hours})
    rows = result.rows or []
    anomalies = [r for r in rows if abs(_f(r.get("z_score"))) >= threshold][:_MAX_REPORTED]

    # Both signals are derived from the FULL scored set, never from `anomalies`:
    # the threshold filter and the display cap both drop rows that are evidence
    # scoring happened.
    has_seasonal = any(r.get("mode") == "seasonal" for r in rows)
    if rows:
        baseline_mode = "seasonal" if has_seasonal else "flat"
    else:
        probe = cache.execute(
            _RECENT_SAMPLES_SQL, {"cluster_id": cluster_id, "hours": hours}
        )
        baseline_mode = "none" if (probe.rows or []) else "no_samples"

    return {
        "cluster_id": cluster_id,
        "hours": hours,
        "threshold": threshold,
        "anomalies": anomalies,
        "total_checked": len(rows),
        "baseline_mode": baseline_mode,
        "note": (
            "robust z = (recent_max − median) / IQR vs a per-hour-of-week baseline; "
            "rows with mode='flat' fall back to 7-day mean/stddev (no seasonal "
            "baseline trained yet → lower confidence). baseline_mode='none' means "
            "samples arrived but no baseline matched (wait for history); "
            "'no_samples' means no recent cluster-level samples at all (check "
            "metric collection). total_checked counts every scored metric; "
            f"anomalies is capped at {_MAX_REPORTED}, strongest first."
        ),
    }
