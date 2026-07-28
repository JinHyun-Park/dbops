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
from mcp_servers.shared.incident_signals import metric_in_clause, resolve_family, signals_for
from mcp_servers.shared.metric_filters import CLUSTER_LEVEL_ONLY
from mcp_servers.shared.schema_diff_util import (
    ALL_ROWS,
    not_seen_note,
    observation_is_complete,
    observed,
)

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
    # An event counter leaving zero (throttles, timed-out cursors) is a discrete
    # abnormal occurrence, not a load fluctuation, so it outweighs a gauge spike.
    "counter_spike": 2.5,
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
        "counter_spikes": 0,
        "slow_queries": 0,
        "elasticache_signals": 0,
    }
    # Sources whose cache table was unavailable (missing/errored). Surfaced
    # separately so a DBA can tell "0 because no rows" from "0 because the
    # collector for that signal isn't deployed".
    skipped = []

    # Which metric_type names to look for depends on the engine: DocumentDB
    # writes cpu_utilization, ElastiCache cache_cpu/engine_cpu, DynamoDB
    # throttle/consumed series. See shared/incident_signals.py.
    family, family_resolved = resolve_family(cache, cluster_id)
    if not family_resolved:
        # Not fatal (events/locks/queries still rank), but the metric set is now
        # a guess, so say so instead of reporting a silent zero.
        skipped.append("engine_family")

    # Filled by the schema source with the shared per-schema confirmation state.
    # Reported at the top level because it qualifies the HIGHEST-weight signal: an
    # empty schema_changes result over a cluster with an unconfirmed schema is not
    # evidence that no DDL happened.
    schema_observation: dict = {}
    candidates.extend(_collect_schema_changes(cache, cluster_id, start_iso, end_iso, anchor,
                                              win, examined, skipped, schema_observation))
    candidates.extend(_collect_events(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))
    candidates.extend(_collect_blocking(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))
    candidates.extend(
        _collect_metric_spikes(
            cache, cluster_id, start_iso, end_iso, baseline_start_iso, anchor, win, examined, skipped, family
        )
    )
    candidates.extend(_collect_slow_queries(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))
    candidates.extend(_collect_elasticache_signals(cache, cluster_id, start_iso, end_iso, anchor, win, examined, skipped))

    # The unknown the accepted cost of this surface creates, in the words the other
    # three consumers use. Composed here so `note` stays a plain expression.
    schema_obs_note = not_seen_note(schema_observation)

    # Rank by score desc, keep the top ~8 and assign 1-based ranks.
    candidates.sort(key=lambda c: c["score"], reverse=True)
    top = candidates[:8]
    for i, cand in enumerate(top, start=1):
        cand["rank"] = i
        cand["score"] = round(cand["score"], 2)

    return {
        "status": "ok",
        "cluster_id": cluster_id,
        # "unknown" (not the relational fallback name) when cluster_meta could
        # not be read: the metric set used was a guess.
        "engine_family": family if family_resolved else "unknown",
        "anchor_time": anchor.isoformat(),
        "window_minutes": win,
        "candidates": top,
        "signals_examined": examined,
        "skipped_sources": skipped,
        # The same `observation` block get_schema_diff, get_schema_history and the
        # dashboard panel carry, from the same shared function, so one state is
        # described one way in all four consumers.
        "schema_observation": schema_observation,
        # Surface the priors + per-candidate score_breakdown so the ranking is
        # explainable (score = base_weight × recency × category factor), not an
        # opaque number a DBA has to trust blindly.
        "scoring_weights": BASE_WEIGHTS,
        "scoring_note": (
            "각 candidate의 score = base_weight(카테고리 prior) × recency(앵커 근접) × "
            "카테고리 인자(event=severity, blocking=block 지속, spike=배수). 자세한 분해는 "
            "candidate.score_breakdown 참고. 우선순위(prior)는 휴리스틱입니다."
        ),
        "note": (
            "ranked by proximity + severity; correlation, not proof, verify before acting"
            + ((" " + schema_obs_note) if schema_obs_note else "")
        ),
    }


# Hoisted out of the function body so a test can EXECUTE it against a real
# engine. It used to be an inline string, and it was the one new statement in
# this tier that no test ran: a column rename inside it left the whole suite
# green while the probe raised on every live call.
#
# ALL_ROWS from the shared contract, not a `FROM schema_snapshots` of its own.
# That is the mechanical rule that stops a seventh pass over this surface (see
# mcp_servers/shared/schema_diff_util.py): the SQL selecting the rows was
# duplicated per consumer, so every pass fixed the copies it owned and the defect
# survived in the ones it did not.
SCHEMA_PRODUCER_PROBE_SQL = (
    "SELECT COUNT(*) AS snapshots, COUNT(DISTINCT schema_name) AS schemas " + ALL_ROWS
)

# EVERY way this source can decline to answer, each under its OWN label. All of
# them mean "we did not look", which is why they all belong in skipped_sources,
# but only ONE of them is a normal state of a young cluster and the rest are
# defects that stay invisible for as long as they share a name. Sharing one label
# is what let a column typo inside the probe survive a full green suite.
#   _SKIP_NO_HISTORY  the reads worked; this cluster has no comparable history
#   _SKIP_READ_ERROR  the schema_snapshots window read itself raised (no
#                     schema_v26 in the cache DB, no permission, cache down)
#   _SKIP_PROBE_ERROR the window read worked and the producer probe raised
# The label is the whole payload: skipped_sources is a bounded string list that
# the agent narrates and frontend/src/app/tasks/page.tsx joins into the "건너뜀"
# line, so the label text IS what a human reads. No exception text goes in it.
_SKIP_NO_HISTORY = "schema_changes"
_SKIP_READ_ERROR = "schema_changes_read_error"
_SKIP_PROBE_ERROR = "schema_changes_probe_error"
# A FOURTH way not to have looked, and the one the accepted cost of this surface
# creates: a schema nobody can currently confirm files no diff row, so an empty
# window is not evidence that no DDL happened. Absence is never resolved to a DROP
# (that produced a phantom mass drop), so the unknown has to appear HERE, in the
# highest-weight source, or the agent under-ranks the most common real cause.
_SKIP_UNCONFIRMED = "schema_changes_unconfirmed_schemas"
# ...and the two OTHER ways the observation can decline, each under its own label
# for the reason the probe labels were split in the previous pass: they lead to
# different operator actions (apply schema_v27 / wait one collection cycle / find
# out why a schema stopped being seen), and a shared label is what let a broken
# read look exactly like a young cluster. Driven apart by
# test_all_six_states_of_this_source_are_distinguishable, which is how this
# conflation was caught inside this very commit.
_SKIP_OBS_ERROR = "schema_changes_observation_error"
_SKIP_UNMIGRATED = "schema_changes_unmigrated"


def _collect_schema_changes(cache, cluster_id, start_iso, end_iso, anchor, win, examined,
                            skipped, observation=None):
    """Schema/DDL changes near the window — the highest-weight category.

    Reads ``schema_snapshots`` (the same table the operations ``get_schema_history``
    tool reads): each row carries a ``diff_from_previous_json`` describing what
    changed at ``snapshot_time``. A deploy/migration landing right before a
    regression is the single most common cause, so a hit here usually ranks at
    or near the top. The table is optional in some deployments, so a missing
    table is swallowed and the source simply contributes nothing.

    A missing TABLE was always reported as ``skipped_sources``, but an EMPTY
    table was not: a cluster whose engine family has no snapshot producer
    contributed 0 candidates and showed up as ``signals_examined: 0``, i.e.
    "we looked and there was no DDL change". Because schema_change carries the
    HIGHEST base weight, that does not just lose one signal, it systematically
    under-ranks the most common real cause. So an empty window is qualified by a
    producer probe: fewer than two snapshots for this cluster means no change
    could have been detected at all, and the source is SKIPPED, not examined.

    Each way of declining carries its OWN label (see the constants above), so the
    five states this source can be in are all distinguishable from the outside:
      window read raised          -> skipped ``schema_changes_read_error``
      empty window, probe raised  -> skipped ``schema_changes_probe_error``
      empty window, no history    -> skipped ``schema_changes``
      empty window, has history   -> NOT skipped, ``signals_examined`` 0
      rows in the window          -> NOT skipped, ``signals_examined`` N
    ``signals_examined`` is pre-seeded to 0 for every source, so on the three
    skipped paths the label is the only thing that tells them apart. Under one
    shared name a broken read looked exactly like a young cluster with no
    history, which is how a column typo in the probe survived a full-suite run:
    every assertion on the negative path passed either way.
    """
    out = []
    # REPLAY of stored diffs, so ALL_ROWS: each stored diff was computed by the
    # producer against a same-scope predecessor by construction. Scope-filtering a
    # replay would erase real DDL history whenever a cluster is re-scoped, which is
    # the opposite failure from the phantom drop.
    sql = (
        "SELECT snapshot_time, schema_name, read_scope, "
        "       diff_from_previous_json AS changes "
        + ALL_ROWS +
        "  AND snapshot_time >= :start_time::timestamptz "
        "  AND snapshot_time < :end_time::timestamptz "
        "  AND diff_from_previous_json IS NOT NULL "
        "  AND diff_from_previous_json::text NOT IN ('{}', '') "
        "ORDER BY snapshot_time DESC"
    )
    params = {"cluster_id": cluster_id, "start_time": start_iso, "end_time": end_iso}
    try:
        rows = cache.execute(sql, params).rows
    except Exception as e:
        print(f"[diagnose_root_cause] schema_changes window read failed: {e}")
        skipped.append(_SKIP_READ_ERROR)
        return out
    # WHAT WAS NOT LOOKED AT, on every path including the one with rows: a schema
    # the collector can no longer confirm is silent here whether or not another
    # schema changed, so this cannot live only in the empty branch.
    obs = observed(lambda s, p: cache.execute(s, p).rows, cluster_id)
    if observation is not None:
        observation.update(obs)
    obs_status = obs.get("status")
    if obs_status == "unavailable":
        skipped.append(_SKIP_OBS_ERROR)
    elif obs_status == "unmigrated":
        skipped.append(_SKIP_UNMIGRATED)
    elif not observation_is_complete(obs) and obs_status != "no_snapshots":
        # `no_snapshots` is the absence of any schema, not an unknown about one, and
        # it already has its own label below.
        skipped.append(_SKIP_UNCONFIRMED)
    if not rows:
        # Empty window: is that "no DDL happened" or "we have no DDL data"?
        # A single baseline snapshot cannot yield a diff row either, so anything
        # under 2 snapshots means this source had no detection capability.
        # Comparability is PER SCHEMA: `snapshots > schemas` holds exactly when at
        # least one schema has a second snapshot to have been diffed against.
        try:
            prows = cache.execute(SCHEMA_PRODUCER_PROBE_SQL,
                                  {"cluster_id": cluster_id}).rows
        except Exception as e:
            print(f"[diagnose_root_cause] schema_changes producer probe failed: {e}")
            skipped.append(_SKIP_PROBE_ERROR)
            return out
        prow = prows[0] if prows else {}
        stored = int(prow.get("snapshots") or 0)
        schemas = int(prow.get("schemas") or 0)
        if stored <= schemas:
            print(f"[diagnose_root_cause] schema_changes source skipped: "
                  f"{stored} snapshot(s) over {schemas} schema(s) for {cluster_id} "
                  "(no comparable history)")
            skipped.append(_SKIP_NO_HISTORY)
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


def _collect_metric_spikes(
    cache, cluster_id, start_iso, end_iso, baseline_start_iso, anchor, win, examined, skipped, family=None
):
    """Metric movement vs the immediately-prior baseline window, engine-aware.

    Which metric_type names exist depends on the engine family (Aurora `cpu` vs
    DocumentDB `cpu_utilization` vs ElastiCache `cache_cpu`), so the names come
    from shared/incident_signals.py. Two paths, one query:

    * GAUGES (cpu, connections, memory, latency): in-window average / prior
      window average. Needs a positive baseline (it is a ratio).
    * COUNTERS (DynamoDB throttles, DocumentDB cursors_timed_out): totals, and
      the baseline is legitimately 0 in healthy operation. Leaving zero IS the
      signal, so these must NOT be dropped by the positive-baseline guard;
      magnitude divides by the metric's noise floor, never by the baseline.

    Both averages/totals come from one grouped query to keep round-trips down.
    """
    out = []
    sets = signals_for(family)
    counters = sets["counters"]
    metric_names = tuple(sets["gauges"]) + tuple(counters)
    in_clause, name_params = metric_in_clause(metric_names)
    sql = f"""
        SELECT metric_type,
               AVG(value) FILTER (
                   WHERE ts >= :start_time::timestamptz AND ts < :end_time::timestamptz
               ) AS window_avg,
               AVG(value) FILTER (
                   WHERE ts >= :baseline_start::timestamptz AND ts < :start_time::timestamptz
               ) AS baseline_avg,
               SUM(value) FILTER (
                   WHERE ts >= :start_time::timestamptz AND ts < :end_time::timestamptz
               ) AS window_sum,
               SUM(value) FILTER (
                   WHERE ts >= :baseline_start::timestamptz AND ts < :start_time::timestamptz
               ) AS baseline_sum
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id
          AND ts >= :baseline_start::timestamptz
          AND ts < :end_time::timestamptz
          AND metric_type {in_clause}
          {CLUSTER_LEVEL_ONLY}
        GROUP BY metric_type
    """
    params = {
        "cluster_id": cluster_id,
        "start_time": start_iso,
        "end_time": end_iso,
        "baseline_start": baseline_start_iso,
        **name_params,
    }
    try:
        rows = cache.execute(sql, params).rows
    except Exception as e:
        print(f"[diagnose_root_cause] metric_spikes source skipped: {e}")
        skipped.append("metric_spikes")
        return out
    spikes = 0
    counter_spikes = 0
    # A spike is a window AGGREGATE, so score its recency at the window midpoint
    # rather than the far edge: using start_iso would floor every spike for a
    # full look-back window and unfairly bury it.
    midpoint = anchor - timedelta(minutes=win / 2.0)
    rf = _recency_factor(midpoint, anchor, win)
    for row in rows:
        metric_type = row.get("metric_type")
        if metric_type in counters:
            cand = _counter_candidate(row, metric_type, counters[metric_type], rf, start_iso, win)
            if cand is not None:
                counter_spikes += 1
                out.append(cand)
            continue
        window_avg = _to_float(row.get("window_avg"))
        baseline_avg = _to_float(row.get("baseline_avg"))
        if window_avg is None or baseline_avg is None or baseline_avg <= 0:
            continue
        ratio = window_avg / baseline_avg
        if ratio < SPIKE_RATIO:
            continue
        spikes += 1
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
    examined["counter_spikes"] = counter_spikes
    return out


def _counter_candidate(row, metric_type, floor, rf, start_iso, win):
    """Candidate for an event counter, or None when it is not a signal.

    Counters (DynamoDB ReadThrottleEvents, DocumentDB DatabaseCursorsTimedOut …)
    sit at exactly 0 when the cluster is healthy, so the gauge path's
    positive-baseline requirement skipped the very metrics a DBA is chasing.
    Here the transition itself is the signal:

      * below the noise floor in the window -> not a signal. This is what stops
        the path from becoming an always-firing alarm: a flat-zero counter has a
        window total of 0, which is below every floor.
      * baseline already nonzero -> require the same SPIKE_RATIO jump as a gauge,
        so a counter that is *steadily* nonzero does not fire every window.
      * magnitude = window_rate / max(floor_rate, baseline_rate). The floor stands
        in for the baseline when the baseline is 0, so this never divides by
        zero, and it is capped like the gauge path so one enormous counter cannot
        outrank every other category.

    RATES, not totals. The incident window is LOOKAHEAD_MINUTES longer than the
    baseline window, so comparing raw totals compares windows of different
    length. That is a fixed +5 minutes, which is negligible at the default
    window_minutes=30 but not at a short one: a perfectly steady 2 events/min
    counter read 30 vs 20 at window_minutes=10 (ratio 1.50) and 20 vs 10 at 5,
    firing with score 3.25, exactly what a genuine from-zero throttle storm
    scores. So every comparison here divides by its own window length while the
    TOTALS stay in the evidence and in the summary, which is what a DBA wants to
    read ("142 throttled requests, previously none"). The floor is a per-window
    count, so it is scaled the same way and from-zero scoring is unchanged.
    """
    window_total = _to_float(row.get("window_sum")) or 0.0
    baseline_total = _to_float(row.get("baseline_sum")) or 0.0
    if window_total < floor:
        return None
    window_minutes = float(win + LOOKAHEAD_MINUTES)
    baseline_minutes = float(win)
    window_rate = window_total / window_minutes
    baseline_rate = baseline_total / baseline_minutes
    if baseline_rate > 0 and window_rate < baseline_rate * SPIKE_RATIO:
        return None
    magnitude = min(window_rate / max(floor / window_minutes, baseline_rate), 2.0)
    score = BASE_WEIGHTS["counter_spike"] * rf * magnitude
    from_zero = baseline_total <= 0
    return {
        "category": "counter_spike",
        "score": score,
        "score_breakdown": {
            "base_weight": BASE_WEIGHTS["counter_spike"],
            "recency_factor": round(rf, 3),
            "magnitude_factor": round(magnitude, 3),
            "noise_floor": floor,
            "formula": "base × recency × (window_rate / max(floor_rate, baseline_rate)), rates per minute",
        },
        "summary": (
            (
                f"{metric_type} appeared during the incident: {round(window_total, 2)} in-window vs "
                "none in the prior baseline window"
            )
            if from_zero
            else (
                f"{metric_type} rose during the incident: {round(window_total, 2)} in-window vs "
                f"{round(baseline_total, 2)} in the prior baseline window "
                f"({round(window_rate / baseline_rate, 2)}x per minute)"
            )
        ),
        "evidence": {
            "metric_type": metric_type,
            "window_total": round(window_total, 3),
            "baseline_total": round(baseline_total, 3),
            # Rates are what the comparison actually used: the windows differ by
            # LOOKAHEAD_MINUTES, so the totals alone do not explain the verdict.
            "window_minutes": window_minutes,
            "baseline_minutes": baseline_minutes,
            "window_rate_per_min": round(window_rate, 3),
            "baseline_rate_per_min": round(baseline_rate, 3),
            "noise_floor": floor,
            "from_zero_baseline": from_zero,
        },
        "when": start_iso,
        "suggested_action": (
            f"{metric_type} is an event counter that is normally 0, so treat any nonzero window as real. "
            "Check capacity/limits (throttling), long-lived cursors or memory pressure depending on the metric."
            if from_zero
            else f"{metric_type} rose sharply above its usual level; check capacity/limits around this window."
        ),
    }


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
