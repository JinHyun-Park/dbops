"""Rank candidate root causes for an incident on a single cluster.

Why this tool exists: the incident server already has building blocks
(``correlate_signals`` UNIONs metrics + events into a flat timeline,
``recent_events``, ``search_logs``), but nothing that RANKS what most likely
caused a regression. A DBA staring at a flat timeline still has to guess. This
tool gathers candidate signals from the pre-collected cache, scores each by
*proximity to the incident* and *severity*, and returns a ranked shortlist so
the DBA can start from the most probable cause.

It deliberately reads only the cache (``CacheClient.execute`` -> Aurora PG
cache), never the live target DB, so it is fast and side-effect free. Every
source query is wrapped in try/except: a missing table (some cache tables are
optional / created lazily) must never crash the whole diagnosis, it just means
that source contributes zero candidates.

Correlation is not causation. The output ``note`` says so explicitly; the
scores rank *suspects*, they do not prove guilt.
"""

from datetime import datetime, timedelta, timezone

from mcp_servers.shared.cache_client import CacheClient

# ---------------------------------------------------------------------------
# Scoring weights. Kept as module constants so the ranking is transparent and
# easy to tune. score = base_weight(category) * recency_factor * severity_factor
#
# base_weight encodes the prior probability that a category is the culprit.
# Schema/DDL changes (deploys, migrations) are the #1 cause of sudden
# regressions, so they get the highest base weight. Discrete operational events
# (failover, reboot, OOM) are next. Lock contention, metric spikes and slow
# queries are often *symptoms* rather than the root cause, so they weigh less.
# ---------------------------------------------------------------------------
BASE_WEIGHTS = {
    "schema_change": 5.0,
    "event": 4.0,
    "blocking": 3.0,
    "metric_spike": 2.0,
    "slow_query": 2.0,
    "elasticache_spike": 2.5,
}

# event_log.severity -> multiplier. Failovers/OOM are usually logged as
# critical/error; warnings are softer; info is background noise.
EVENT_SEVERITY_FACTOR = {
    "critical": 1.5,
    "error": 1.3,
    "warning": 0.9,
    "info": 0.5,
}

# Lookahead after the anchor: a cause can show up a few minutes before the
# symptom is noticed, but the symptom can also slightly precede the log entry.
LOOKAHEAD_MINUTES = 5

# A metric is a "spike" if its in-window average is at least this multiple of
# the immediately-prior baseline window.
SPIKE_RATIO = 1.5

# Recency decay: 1.0 at the anchor, floored at this value at the window edge.
RECENCY_FLOOR = 0.3


def _parse_ts(value):
    """Best-effort parse of an ISO 8601 timestamp into an aware datetime.

    The cache returns timestamps as strings (RDS Data API), and callers may
    pass ``around_time`` in a few shapes (with ``Z``, with offset, or naive).
    Returns ``None`` if it cannot be parsed so callers can skip that row rather
    than blow up the whole diagnosis.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    # Normalize trailing Z and space-separated date/time for fromisoformat.
    text = text.replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _recency_factor(when, anchor, window_minutes):
    """Linear decay from 1.0 at the anchor down to RECENCY_FLOOR at the edge.

    Distance is measured in minutes from the anchor; the "edge" is the wider of
    the look-back window and the lookahead so a candidate is never clamped
    below the floor while still inside the examined range.
    """
    when_dt = _parse_ts(when)
    if when_dt is None or anchor is None:
        return RECENCY_FLOOR
    distance_min = abs((when_dt - anchor).total_seconds()) / 60.0
    edge = float(max(window_minutes, LOOKAHEAD_MINUTES))
    if edge <= 0:
        return 1.0
    factor = 1.0 - (1.0 - RECENCY_FLOOR) * min(distance_min / edge, 1.0)
    return max(RECENCY_FLOOR, factor)


def _resolve_anchor(cache, around_time):
    """Return the anchor datetime. Empty around_time => cache NOW().

    Anchoring on the cache's own clock (rather than the agent's) keeps the
    recency math consistent with the timestamps stored in the cache. Falls back
    to the local clock if the NOW() probe fails for any reason.
    """
    parsed = _parse_ts(around_time)
    if parsed is not None:
        return parsed
    try:
        res = cache.execute("SELECT NOW() AS now")
        if res.rows:
            now = _parse_ts(res.rows[0].get("now"))
            if now is not None:
                return now
    except Exception as e:  # pragma: no cover - defensive
        print(f"[diagnose_root_cause] NOW() probe failed: {e}")
    return datetime.now(timezone.utc)


def diagnose_root_cause_impl(
    cache: CacheClient,
    cluster_id: str,
    around_time: str = "",
    window_minutes: int = 30,
) -> dict:
    """Gather candidate root-cause signals around an incident and rank them.

    Args:
        cache: cache client (reads the Aurora PG cache, not the target DB).
        cluster_id: target cluster.
        around_time: ISO 8601 incident time; empty => anchor on cache NOW().
        window_minutes: minutes to look back before the anchor.

    Returns a dict with the anchor, a ranked ``candidates`` list (top ~8 by
    score desc), a ``signals_examined`` count per source, and a ``note``.
    """
    # A non-empty but unparseable around_time must NOT silently become NOW() —
    # that would diagnose the wrong window and quietly mislead the DBA.
    if around_time and _parse_ts(around_time) is None:
        return {
            "status": "error",
            "cluster_id": cluster_id,
            "reason": (
                f"could not parse around_time {around_time!r} — pass ISO 8601 "
                "(e.g. 2026-06-08T14:30:00Z) or leave it empty to anchor on now"
            ),
        }

    anchor = _resolve_anchor(cache, around_time)
    win = max(1, int(window_minutes))
    start_dt = anchor - timedelta(minutes=win)
    end_dt = anchor + timedelta(minutes=LOOKAHEAD_MINUTES)
    # Baseline = the window immediately BEFORE the look-back window.
    baseline_start_dt = start_dt - timedelta(minutes=win)

    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()
    baseline_start_iso = baseline_start_dt.isoformat()

    candidates = []
    examined = {
        "schema_changes": 0,
        "events": 0,
        "blocking": 0,
        "metric_spikes": 0,
        "slow_queries": 0,
    }
    # Sources whose cache table was unavailable (missing/errored). Surfaced
    # separately so a DBA can tell "0 because no rows" from "0 because the
    # collector for that signal isn't deployed".
    skipped = []

    candidates.extend(_collect_schema_changes(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))
    candidates.extend(_collect_events(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))
    candidates.extend(_collect_blocking(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))
    candidates.extend(
        _collect_metric_spikes(cache, cluster_id, start_iso, end_iso, baseline_start_iso, anchor, win, examined, skipped)
    )
    candidates.extend(_collect_slow_queries(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))
    candidates.extend(_collect_elasticache_signals(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))

    # Rank by score desc, keep the top ~8 and assign 1-based ranks.
    candidates.sort(key=lambda c: c["score"], reverse=True)
    top = candidates[:8]
    for i, cand in enumerate(top, start=1):
        cand["rank"] = i
        cand["score"] = round(cand["score"], 2)

    return {
        "status": "ok",
        "cluster_id": cluster_id,
        "anchor_time": anchor.isoformat(),
        "window_minutes": win,
        "candidates": top,
        "signals_examined": examined,
        "skipped_sources": skipped,
        # Surface the priors + per-candidate score_breakdown so the ranking is
        # explainable (score = base_weight × recency × category factor), not an
        # opaque number a DBA has to trust blindly.
        "scoring_weights": BASE_WEIGHTS,
        "scoring_note": (
            "각 candidate의 score = base_weight(카테고리 prior) × recency(앵커 근접) × "
            "카테고리 인자(event=severity, blocking=block 지속, spike=배수). 자세한 분해는 "
            "candidate.score_breakdown 참고. 우선순위(prior)는 휴리스틱입니다."
        ),
        "note": "ranked by proximity + severity; correlation, not proof — verify before acting",
    }


def _collect_schema_changes(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped):
    """Schema/DDL changes near the window — the highest-weight category.

    Reads ``schema_snapshots`` (the same table the operations ``get_schema_history``
    tool reads): each row carries a ``diff_from_previous_json`` describing what
    changed at ``snapshot_time``. A deploy/migration landing right before a
    regression is the single most common cause, so a hit here usually ranks at
    or near the top. The table is optional in some deployments, so a missing
    table is swallowed and the source simply contributes nothing.
    """
    out = []
    sql = """
        SELECT snapshot_time, schema_name, diff_from_previous_json AS changes
        FROM schema_snapshots
        WHERE cluster_id = :cluster_id
          AND snapshot_time >= :start_time::timestamptz
          AND snapshot_time < :end_time::timestamptz
          AND diff_from_previous_json IS NOT NULL
          AND diff_from_previous_json::text NOT IN ('{}', '')
        ORDER BY snapshot_time DESC
    """
    params = {"cluster_id": cluster_id, "start_time": start_iso, "end_time": end_iso}
    try:
        rows = cache.execute(sql, params).rows
    except Exception as e:
        print(f"[diagnose_root_cause] schema_changes source skipped: {e}")
        skipped.append("schema_changes")
        return out
    examined["schema_changes"] = len(rows)
    for row in rows:
        when = row.get("snapshot_time")
        rf = _recency_factor(when, anchor, win)
        score = BASE_WEIGHTS["schema_change"] * rf
        schema_name = row.get("schema_name") or "?"
        out.append(
            {
                "category": "schema_change",
                "score": score,
                "score_breakdown": {
                    "base_weight": BASE_WEIGHTS["schema_change"],
                    "recency_factor": round(rf, 3),
                    "formula": "base × recency",
                },
                "summary": f"Schema/DDL change in '{schema_name}' near the incident",
                "evidence": {
                    "schema_name": schema_name,
                    "snapshot_time": when,
                    "diff": row.get("changes"),
                },
                "when": when,
                "suggested_action": "Review the schema diff; correlate the deploy/migration with the symptom onset and consider a rollback.",
            }
        )
    return out


def _collect_events(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped):
    """Discrete operational events from ``event_log`` (failover, reboot, OOM…).

    Weighted by ``severity`` (critical/error high, warning medium, info low) so
    a critical failover beats an informational housekeeping message even at the
    same distance from the anchor.
    """
    out = []
    sql = """
        SELECT event_time, event_type, message, severity, source
        FROM event_log
        WHERE cluster_id = :cluster_id
          AND event_time >= :start_time::timestamptz
          AND event_time < :end_time::timestamptz
        ORDER BY event_time DESC
    """
    params = {"cluster_id": cluster_id, "start_time": start_iso, "end_time": end_iso}
    try:
        rows = cache.execute(sql, params).rows
    except Exception as e:
        print(f"[diagnose_root_cause] events source skipped: {e}")
        skipped.append("events")
        return out
    examined["events"] = len(rows)
    for row in rows:
        when = row.get("event_time")
        severity = str(row.get("severity") or "info").lower()
        sev_factor = EVENT_SEVERITY_FACTOR.get(severity, 0.7)
        rf = _recency_factor(when, anchor, win)
        score = BASE_WEIGHTS["event"] * rf * sev_factor
        event_type = row.get("event_type") or "event"
        out.append(
            {
                "category": "event",
                "score": score,
                "score_breakdown": {
                    "base_weight": BASE_WEIGHTS["event"],
                    "recency_factor": round(rf, 3),
                    "severity_factor": sev_factor,
                    "formula": "base × recency × severity",
                },
                "summary": f"{severity.upper()} event '{event_type}' near the incident",
                "evidence": {
                    "event_type": event_type,
                    "severity": severity,
                    "source": row.get("source"),
                    "message": row.get("message"),
                    "event_time": when,
                },
                "when": when,
                "suggested_action": "Inspect the event detail; failover/reboot/OOM events often explain abrupt connection or latency changes.",
            }
        )
    return out


def _collect_blocking(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped):
    """Lock contention from ``blocking_locks``.

    A long ``blocked_duration_sec`` means a blocking transaction stalled others,
    which can look like a cluster-wide slowdown. Longer blocks raise the score
    (capped) so the worst offender surfaces first.
    """
    out = []
    sql = """
        SELECT snapshot_time, blocked_pid, blocking_pid, blocked_query, blocking_query,
               blocked_duration_sec, blocked_user, blocking_user
        FROM blocking_locks
        WHERE cluster_id = :cluster_id
          AND snapshot_time >= :start_time::timestamptz
          AND snapshot_time < :end_time::timestamptz
        ORDER BY blocked_duration_sec DESC NULLS LAST
    """
    params = {"cluster_id": cluster_id, "start_time": start_iso, "end_time": end_iso}
    try:
        rows = cache.execute(sql, params).rows
    except Exception as e:
        print(f"[diagnose_root_cause] blocking source skipped: {e}")
        skipped.append("blocking")
        return out
    examined["blocking"] = len(rows)
    for row in rows:
        when = row.get("snapshot_time")
        duration = row.get("blocked_duration_sec") or 0
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = 0.0
        # Duration factor: 1.0 at 0s rising toward ~2.0 for long (>=60s) blocks.
        duration_factor = 1.0 + min(duration / 60.0, 1.0)
        rf = _recency_factor(when, anchor, win)
        score = BASE_WEIGHTS["blocking"] * rf * duration_factor
        out.append(
            {
                "category": "blocking",
                "score": score,
                "score_breakdown": {
                    "base_weight": BASE_WEIGHTS["blocking"],
                    "recency_factor": round(rf, 3),
                    "duration_factor": round(duration_factor, 3),
                    "formula": "base × recency × duration",
                },
                "summary": f"Lock contention: pid {row.get('blocked_pid')} blocked {round(duration, 1)}s by pid {row.get('blocking_pid')}",
                "evidence": {
                    "blocked_pid": row.get("blocked_pid"),
                    "blocking_pid": row.get("blocking_pid"),
                    "blocked_duration_sec": duration,
                    "blocked_query": row.get("blocked_query"),
                    "blocking_query": row.get("blocking_query"),
                    "blocked_user": row.get("blocked_user"),
                    "blocking_user": row.get("blocking_user"),
                    "snapshot_time": when,
                },
                "when": when,
                "suggested_action": "Identify the blocking transaction; long blocking chains may need a terminated session or an index/query fix.",
            }
        )
    return out


def _collect_metric_spikes(cache, cluster_id, start_iso, end_iso, baseline_start_iso, anchor, win, examined, skipped):
    """Metric spikes vs the immediately-prior baseline window.

    For each of aas/cpu/connections we compare the in-window average to the
    average of the window immediately BEFORE it. A jump of >= SPIKE_RATIO (and a
    positive baseline) is a spike candidate. We compute both averages in a
    single grouped query per metric to keep round-trips down.
    """
    out = []
    sql = """
        SELECT metric_type,
               AVG(value) FILTER (
                   WHERE ts >= :start_time::timestamptz AND ts < :end_time::timestamptz
               ) AS window_avg,
               AVG(value) FILTER (
                   WHERE ts >= :baseline_start::timestamptz AND ts < :start_time::timestamptz
               ) AS baseline_avg
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id
          AND ts >= :baseline_start::timestamptz
          AND ts < :end_time::timestamptz
          AND metric_type IN ('aas', 'cpu', 'db_connections')
          AND (dimensions IS NULL OR NOT jsonb_exists(dimensions, 'instance'))
        GROUP BY metric_type
    """
    params = {
        "cluster_id": cluster_id,
        "start_time": start_iso,
        "end_time": end_iso,
        "baseline_start": baseline_start_iso,
    }
    try:
        rows = cache.execute(sql, params).rows
    except Exception as e:
        print(f"[diagnose_root_cause] metric_spikes source skipped: {e}")
        skipped.append("metric_spikes")
        return out
    spikes = 0
    for row in rows:
        metric_type = row.get("metric_type")
        window_avg = _to_float(row.get("window_avg"))
        baseline_avg = _to_float(row.get("baseline_avg"))
        if window_avg is None or baseline_avg is None or baseline_avg <= 0:
            continue
        ratio = window_avg / baseline_avg
        if ratio < SPIKE_RATIO:
            continue
        spikes += 1
        # A spike is an in-window AVERAGE, so score its recency at the window
        # midpoint rather than the far edge — using start_iso would floor every
        # spike for a full look-back window and unfairly bury it.
        midpoint = anchor - timedelta(minutes=win / 2.0)
        rf = _recency_factor(midpoint, anchor, win)
        spike_factor = min(ratio / SPIKE_RATIO, 2.0)
        score = BASE_WEIGHTS["metric_spike"] * rf * spike_factor
        out.append(
            {
                "category": "metric_spike",
                "score": score,
                "score_breakdown": {
                    "base_weight": BASE_WEIGHTS["metric_spike"],
                    "recency_factor": round(rf, 3),
                    "spike_factor": round(spike_factor, 3),
                    "formula": "base × recency × spike_magnitude",
                },
                "summary": f"{metric_type} spiked {round(ratio, 2)}x vs prior baseline ({round(baseline_avg, 2)} -> {round(window_avg, 2)})",
                "evidence": {
                    "metric_type": metric_type,
                    "window_avg": round(window_avg, 3),
                    "baseline_avg": round(baseline_avg, 3),
                    "ratio": round(ratio, 3),
                },
                "when": start_iso,
                "suggested_action": f"Investigate what drove {metric_type} up around this window (load change, plan regression, runaway query).",
            }
        )
    examined["metric_spikes"] = spikes
    return out


def _collect_slow_queries(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped):
    """Top slow / heavy queries in the window from ``query_stats``.

    Surfaces the top 3 offenders by ``total_time_ms``. These are often symptoms
    of an upstream cause (a missing index after a schema change, a spike in
    calls), hence a medium weight, but they tell the DBA *where* the pain is.
    """
    out = []
    sql = """
        SELECT query_hash, query_text, calls, total_time_ms, mean_time_ms, snapshot_time
        FROM query_stats
        WHERE cluster_id = :cluster_id
          AND snapshot_time >= :start_time::timestamptz
          AND snapshot_time < :end_time::timestamptz
        ORDER BY total_time_ms DESC NULLS LAST
        LIMIT 3
    """
    params = {"cluster_id": cluster_id, "start_time": start_iso, "end_time": end_iso}
    try:
        rows = cache.execute(sql, params).rows
    except Exception as e:
        print(f"[diagnose_root_cause] slow_queries source skipped: {e}")
        skipped.append("slow_queries")
        return out
    examined["slow_queries"] = len(rows)
    for row in rows:
        when = row.get("snapshot_time")
        total_ms = _to_float(row.get("total_time_ms")) or 0.0
        query_text = (row.get("query_text") or "").strip()
        snippet = (query_text[:120] + "…") if len(query_text) > 120 else query_text
        rf = _recency_factor(when, anchor, win)
        score = BASE_WEIGHTS["slow_query"] * rf
        out.append(
            {
                "category": "slow_query",
                "score": score,
                "score_breakdown": {
                    "base_weight": BASE_WEIGHTS["slow_query"],
                    "recency_factor": round(rf, 3),
                    "formula": "base × recency",
                },
                "summary": f"Heavy query ({round(total_ms, 1)}ms total, {row.get('calls')} calls): {snippet}",
                "evidence": {
                    "query_hash": row.get("query_hash"),
                    "query_text": query_text,
                    "calls": row.get("calls"),
                    "total_time_ms": total_ms,
                    "mean_time_ms": _to_float(row.get("mean_time_ms")),
                    "snapshot_time": when,
                },
                "when": when,
                "suggested_action": "EXPLAIN the query; check for a missing index or a plan regression coinciding with the incident.",
            }
        )
    return out


def _collect_elasticache_signals(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped):
    """ElastiCache cache-specific signals from metric_snapshots: eviction spikes
    and replication-lag spikes near the incident. Engine-safe — non-ElastiCache
    clusters have no such rows, so this yields nothing."""
    out = []
    sql = """
        SELECT ts, metric_type, value
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id
          AND metric_type IN ('evictions', 'replication_lag')
          AND ts >= :start_time::timestamptz AND ts < :end_time::timestamptz
          AND (dimensions IS NULL OR dimensions::text = '{}')
          AND ((metric_type = 'evictions' AND value > 100)
               OR (metric_type = 'replication_lag' AND value >= 100))
        ORDER BY value DESC
    """
    params = {"cluster_id": cluster_id, "start_time": start_iso, "end_time": end_iso}
    try:
        rows = cache.execute(sql, params).rows
    except Exception as e:
        print(f"[diagnose_root_cause] elasticache_signals source skipped: {e}")
        skipped.append("elasticache_signals")
        return out
    examined["elasticache_signals"] = len(rows)
    for row in rows:
        when = row.get("ts")
        mtype = row.get("metric_type")
        value = row.get("value")
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        rf = _recency_factor(when, anchor, win)
        score = BASE_WEIGHTS["elasticache_spike"] * rf
        if mtype == "replication_lag":
            title = "ElastiCache Replication Lag Spike"
            action = "Check write load / failover; replication lag often coincides with a primary failover or load surge."
        else:
            title = "ElastiCache Eviction Spike"
            action = "Memory pressure — evictions spiking suggests the working set exceeds capacity; check maxmemory-policy and node size."
        out.append({
            "category": "elasticache_spike",
            "score": score,
            "score_breakdown": {
                "base_weight": BASE_WEIGHTS["elasticache_spike"],
                "recency_factor": round(rf, 3),
                "formula": "base × recency",
            },
            "summary": title,
            "evidence": {"metric_type": mtype, "value": value, "metric_time": when},
            "when": when,
            "suggested_action": action,
        })
    return out


def _to_float(value):
    """Coerce a cache value to float, or None if it is null/unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
