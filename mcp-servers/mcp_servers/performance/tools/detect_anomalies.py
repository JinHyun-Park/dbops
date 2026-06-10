"""detect_anomalies — seasonal, robust metric anomaly detection.

Replaces the old flat 7-day mean/stddev z-score (which ignored daily/weekly
seasonality and blew up on a cluster with a few legitimate spikes per day) with
the SAME robust seasonal baseline the dashboard already uses: a per-hour-of-week
median + IQR trained into ``metric_baselines`` by the pg_baseline_trainer
collector. Robust z = (recent_max - median) / IQR — the IQR doesn't inflate on
outliers, so the score is stable.

When no seasonal baseline exists yet for the current hour-of-week bucket
(cold-start: < ~2 weeks of history) we fall back to the legacy flat mean/stddev
and tag the row ``mode='flat'`` so the caller knows the finding is lower
confidence. Each row carries its baseline + sample_count so the agent can
explain WHY something is anomalous.
"""

from mcp_servers.shared.cache_client import CacheClient

# Robust seasonal baseline (median/IQR per hour-of-week) with a flat
# mean/stddev fallback. Mirrors the dashboard's seasonal anomaly query so the
# chat agent and the dashboard never disagree on what's anomalous.
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
LIMIT 50
"""


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
    result = cache.execute(_ANOMALY_SQL, {"cluster_id": cluster_id, "hours": hours})
    rows = result.rows or []
    anomalies = [r for r in rows if abs(_f(r.get("z_score"))) >= threshold]

    # Confidence signal: did we score against the seasonal baseline or fall back
    # to the flat one? If every row is flat, the cluster lacks a trained
    # baseline yet (cold-start) and findings are lower confidence.
    has_seasonal = any(r.get("mode") == "seasonal" for r in rows)
    baseline_mode = "seasonal" if has_seasonal else ("flat" if rows else "none")

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
            "baseline trained yet → lower confidence)."
        ),
    }
