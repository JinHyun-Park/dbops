"""Tests for diagnose_root_cause_impl.

The impl issues one cache.execute call per source. The fake DISPATCHES ON SQL
rather than on call ORDER: a positional side_effect list breaks, silently and
misleadingly, the moment a source gains a statement (the schema source gained the
two shared observation statements in the sixth pass over that surface, which
shifted every later source's rows by two and made a working tool look broken).
Assertions target the RETURN structure (ranks/categories/scores/
signals_examined), never the SQL strings, so they survive SQL tweaks. The one
documented exception is test_the_metric_spike_query_selects_what_the_scorer_reads:
the fake never executes SQL, so a column the Python scorer reads BY NAME is
otherwise unverifiable here, and dropping it degrades every peak candidate
identically, which no return-value assertion can distinguish.

Per-family metric sets and the zero-baseline counter path have their own file:
test_diagnose_family_signals.py.
"""

from unittest.mock import MagicMock

from mcp_servers.incident.tools.diagnose_root_cause import (
    SELF_EVENT_SOURCES,
    diagnose_root_cause_impl,
)
from mcp_servers.shared.models import QueryResult

ANCHOR = "2024-01-01T12:00:00Z"


def _qr(rows):
    return QueryResult(columns=list(rows[0].keys()) if rows else [], rows=rows, row_count=len(rows))


def _empty():
    return QueryResult(columns=[], rows=[], row_count=0)


def _dispatching_cache(**by_source):
    """Return the rows for whichever source is asking, keyed on a fragment of its
    SQL. Anything unclaimed comes back empty, which is what a cluster with no rows
    for that signal looks like."""
    def execute(sql, params=None):
        if "FROM cluster_meta" in sql:
            # PostgreSQL: schema snapshots are collected for this dialect only.
            return _qr([{"engine": "aurora-postgresql"}])
        if "read_scope IS NOT NULL" in sql:
            return _qr([{"read_scope": "dbops/16384"}])
        if "holds_tables" in sql:
            return by_source.get("observation", _qr([
                {"schema_name": "public", "read_scope": "dbops/16384",
                 "last_seen": ANCHOR, "holds_tables": "y", "age_sec": 60}]))
        for marker, rows in by_source.items():
            if marker != "observation" and marker in sql:
                return rows
        return _empty()
    cache = MagicMock()
    cache.execute.side_effect = execute
    return cache


def test_ranks_schema_change_event_and_metric_spike():
    cache = _dispatching_cache(
        cluster_meta=_qr([{"engine": "aurora-postgresql"}]),
        # schema change right at the anchor -> should rank at/near the top
        diff_from_previous_json=_qr([
            {
                "snapshot_time": "2024-01-01T11:59:00Z",
                "schema_name": "public",
                "changes": '{"added_index": "idx_orders_status"}',
            }
        ]),
        # a critical event a couple minutes before the anchor
        event_log=_qr([
            {
                "event_time": "2024-01-01T11:58:00Z",
                "event_type": "failover",
                "message": "Writer failover started",
                "severity": "critical",
                "source": "rds-event",
            }
        ]),
        # cpu spiked 3x vs baseline (blocking_locks is left empty). The marker is
        # `window_avg`, not `metric_snapshots`: the elasticache source reads the same
        # table, so the table name matches two statements and would feed both.
        window_avg=_qr([
            {"metric_type": "cpu", "window_avg": 90.0, "baseline_avg": 30.0},
            {"metric_type": "connections", "window_avg": 50.0, "baseline_avg": 48.0},
        ]),
        query_stats=_qr([
            # The GROUPED shape the collector now selects: one row per query_hash
            # with the cumulative readings it measures a window delta between.
            # 54000 in-window against a 12000 pre-window reading = 42000ms spent
            # here, not the 54000 lifetime total the old shape reported as this
            # incident's cost.
            {
                "query_hash": "abc123",
                "query_text": "SELECT * FROM orders WHERE status = $1",
                "win_max": 54000.0,
                "win_min": 48000.0,
                "pre_max": 12000.0,
                "calls_max": 1200,
                "calls_min": 1100,
                "pre_calls": 300,
                "mean_time_ms": 45.0,
                "snapshot_time": "2024-01-01T11:57:00Z",
                "snapshots": 6,
            }
        ]),
    )

    result = diagnose_root_cause_impl(cache, cluster_id="prod-pg-1", around_time=ANCHOR, window_minutes=30)

    assert result["cluster_id"] == "prod-pg-1"
    assert result["anchor_time"].startswith("2024-01-01T12:00:00")
    assert result["window_minutes"] == 30
    assert "correlation, not proof" in result["note"]

    cands = result["candidates"]
    # schema_change + critical event + 1 metric spike (cpu only) + 1 slow query = 4
    assert len(cands) == 4

    # ranks are 1..N, contiguous and in score-descending order
    assert [c["rank"] for c in cands] == [1, 2, 3, 4]
    scores = [c["score"] for c in cands]
    assert scores == sorted(scores, reverse=True)

    categories = [c["category"] for c in cands]
    assert "schema_change" in categories
    assert "event" in categories
    assert "metric_spike" in categories
    assert "slow_query" in categories

    # schema change sits right at the anchor with the highest base weight, so it
    # ranks at/near the top (a critical failover can edge it out via severity).
    top_two = [c["category"] for c in cands[:2]]
    assert "schema_change" in top_two

    # every candidate carries the expected fields + an explainable breakdown
    for c in cands:
        assert isinstance(c["score"], float)
        assert c["summary"]
        assert c["evidence"]
        assert c["suggested_action"]
        bd = c["score_breakdown"]
        assert bd["base_weight"] > 0
        assert 0 < bd["recency_factor"] <= 1.0
        assert "formula" in bd

    # the event candidate's breakdown exposes its severity multiplier
    event = next(c for c in cands if c["category"] == "event")
    assert event["score_breakdown"]["severity_factor"] == 1.5  # critical

    # top-level scoring transparency
    assert result["scoring_weights"]["schema_change"] == 5.0
    assert "score_breakdown" in result["scoring_note"]

    # connections was NOT a spike (50/48 < 1.5) -> only cpu counted
    assert result["signals_examined"] == {
        "schema_changes": 1,
        "events": 1,
        "blocking": 0,
        "metric_spikes": 1,
        "counter_spikes": 0,
        "slow_queries": 1,
        "elasticache_signals": 0,
    }


def test_empty_cache_returns_no_candidates():
    cache = MagicMock()
    cache.execute.side_effect = [_qr([{"engine": "aurora-postgresql"}]), _empty(), _empty(), _empty(), _empty(), _empty()]

    result = diagnose_root_cause_impl(cache, cluster_id="prod-pg-1", around_time=ANCHOR)

    assert result["candidates"] == []
    assert "correlation, not proof" in result["note"]
    assert result["signals_examined"] == {
        "schema_changes": 0,
        "events": 0,
        "blocking": 0,
        "metric_spikes": 0,
        "counter_spikes": 0,
        "slow_queries": 0,
        "elasticache_signals": 0,
    }


def test_missing_table_on_one_source_still_ranks_others():
    cache = MagicMock()
    # schema_snapshots table is absent -> that execute raises; the rest succeed.
    cache.execute.side_effect = [
        _qr([{"engine": "aurora-postgresql"}]),
        Exception('relation "schema_snapshots" does not exist'),
        _qr([
            {
                "event_time": "2024-01-01T11:55:00Z",
                "event_type": "reboot",
                "message": "Instance rebooted",
                "severity": "error",
                "source": "rds-event",
            }
        ]),
        _empty(),
        _empty(),
        _empty(),
    ]

    result = diagnose_root_cause_impl(cache, cluster_id="prod-pg-1", around_time=ANCHOR)

    # The missing schema source is counted 0 but does not crash the diagnosis.
    assert result["signals_examined"]["schema_changes"] == 0
    cands = result["candidates"]
    assert len(cands) == 1
    assert cands[0]["category"] == "event"
    assert cands[0]["rank"] == 1
    assert cands[0]["score"] > 0


def test_invalid_around_time_returns_error_not_now_fallback():
    """A non-empty but unparseable around_time must error, not silently
    diagnose the current time (which would mislead the DBA)."""
    from mcp_servers.incident.tools.diagnose_root_cause import diagnose_root_cause_impl

    cache = MagicMock()
    out = diagnose_root_cause_impl(cache, cluster_id="prod-pg-1", around_time="last tuesday")
    assert out["status"] == "error"
    assert "around_time" in out["reason"]
    # must not have run any diagnosis queries
    cache.execute.assert_not_called()


def test_unavailable_source_is_reported_in_skipped_sources():
    """If a source's cache table errors, it's listed in skipped_sources so the
    caller can distinguish 'no rows' from 'collector not deployed'."""
    from mcp_servers.incident.tools.diagnose_root_cause import diagnose_root_cause_impl
    from mcp_servers.shared.models import QueryResult

    empty = QueryResult(columns=[], rows=[], row_count=0)

    def _side_effect(sql, params=None):
        # First call is the NOW() anchor probe; the schema_changes query raises.
        if "NOW()" in sql:
            return QueryResult(columns=["now"], rows=[{"now": "2026-06-08T12:00:00+00:00"}], row_count=1)
        if "schema_snapshots" in sql:
            raise RuntimeError("relation \"schema_snapshots\" does not exist")
        return empty

    cache = MagicMock()
    cache.execute.side_effect = _side_effect
    out = diagnose_root_cause_impl(cache, cluster_id="prod-pg-1")
    assert out["status"] == "ok"
    # The label says WHICH failure: a read that raised, not a cluster whose
    # history simply has nothing comparable in it. `schema_changes` alone means
    # the latter, and the two were byte-identical until round 4.
    assert "schema_changes_read_error" in out["skipped_sources"]
    assert "schema_changes" not in out["skipped_sources"]
    assert out["signals_examined"]["schema_changes"] == 0


def test_a_brief_saturation_is_not_diluted_away_by_the_window_average():
    """A CPU incident must produce CPU evidence, using the numbers that failed to.

    These are the exact values from the one real auto-RCA this deployment has ever
    produced (2026-08-30), whose own title was "PG high CPU: cpu = 100.00 > 80.0":

        window_avg   = 55.147      (the 30 minutes before the anchor)
        baseline_avg = 50.880      (the 30 minutes before that)
        ratio        = 1.0839  <  SPIKE_RATIO 1.5
        window_max   = 100.0       (peak_ratio = 1.9654)

    The average-only gate dropped it, so that RCA reported
    signals_examined.metric_spikes = 0 for a CPU incident and fell back to slow-query
    rows as its only real signal. The longer the window, the more certainly a genuine
    spike disappears into the mean.
    """
    # Six samples: five around 46 and one at 100, which is what an average of 55.147
    # with a maximum of 100 reconstructs to. The real incident WAS one CloudWatch
    # 5-minute sample at full saturation, so this case also pins the lone-sample path.
    cache = _dispatching_cache(
        window_max=_qr([
            {"metric_type": "cpu", "window_avg": 55.147, "baseline_avg": 50.880,
             "window_sum": 330.882, "baseline_sum": None, "window_max": 100.0,
             "window_samples": 6},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)

    assert res["status"] == "ok"
    assert res["signals_examined"]["metric_spikes"] == 1, (
        "a 100% CPU peak must be examined as a spike; the window average alone hides it"
    )
    spike = next(c for c in res["candidates"] if c["category"] == "metric_spike")

    # The score has to come from the PEAK, not the diluted average: off the average the
    # spike_factor would be 1.0839/1.5 = 0.72, which would rank a full saturation BELOW
    # a metric that merely drifted 1.5x: in the list, and buried.
    assert spike["score_breakdown"]["qualified_by"] == "peak"
    assert spike["score_breakdown"]["spike_factor"] > 1.0839 / 1.5, spike["score_breakdown"]
    assert spike["evidence"]["window_max"] == 100.0
    assert round(spike["evidence"]["peak_ratio"], 3) == 1.965

    # One sample carries the whole excess, so it is admitted at reduced confidence and
    # says so. peak_ratio 1.9654 / 1.5 = 1.3103, times LONE_PEAK_CONFIDENCE 0.75.
    assert spike["score_breakdown"]["lone_sample"] is True
    assert round(spike["score_breakdown"]["spike_factor"], 3) == 0.983
    assert spike["evidence"]["window_samples"] == 6

    # The summary must say which fact was used, or it contradicts the score.
    assert "peaked" in spike["summary"] and "max 100.0" in spike["summary"], spike["summary"]
    assert "single sample of 6" in spike["summary"], spike["summary"]


def test_a_corroborated_peak_is_not_discounted_as_a_lone_sample():
    """Two of six samples saturated: the maximum is supported, so full weight.

    This is the discriminating partner of the test above. Both enter on the peak path
    with by_avg False; only this one keeps the undiscounted spike_factor. Without it,
    LONE_PEAK_CONFIDENCE could be applied to every peak and both tests still pass.
    Values [50, 50, 50, 50, 100, 100] over a baseline of 50: window_avg 66.667,
    ratio 1.333 (below the gate), peak_ratio 2.0, and the top sample carries half the
    window's excess rather than all of it.
    """
    cache = _dispatching_cache(
        window_max=_qr([
            {"metric_type": "cpu", "window_avg": 66.667, "baseline_avg": 50.0,
             "window_sum": 400.0, "baseline_sum": None, "window_max": 100.0,
             "window_samples": 6},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    spike = next(c for c in res["candidates"] if c["category"] == "metric_spike")
    assert spike["score_breakdown"]["qualified_by"] == "peak"
    assert spike["score_breakdown"]["lone_sample"] is False
    # 2.0 / 1.5, undiscounted.
    assert round(spike["score_breakdown"]["spike_factor"], 3) == 1.333
    assert "sample" not in spike["summary"], spike["summary"]


def test_a_single_outlier_scores_below_the_same_peak_corroborated():
    """One corrupt or bursty reading must not outrank corroborated evidence.

    A lone 300 among five samples at 50 has a peak_ratio of 6.0, far above the
    corroborated 2.0 saturation in the test above, so magnitude alone would put it on
    top. It is admitted (in-window statistics at 5-minute cadence genuinely cannot
    separate a bad reading from a real 5-minute excursion) but capped and flagged.
    """
    lone = _dispatching_cache(
        window_max=_qr([
            {"metric_type": "cpu", "window_avg": 91.667, "baseline_avg": 50.0,
             "window_sum": 550.0, "baseline_sum": None, "window_max": 300.0,
             "window_samples": 6},
        ]),
    )
    res = diagnose_root_cause_impl(lone, "c1", around_time=ANCHOR, window_minutes=30)
    spike = next(c for c in res["candidates"] if c["category"] == "metric_spike")
    assert spike["score_breakdown"]["lone_sample"] is True
    # Capped at 2.0 then discounted, so a 6x lone reading cannot beat a 2x corroborated
    # spike by magnitude: 1.5 vs the 2.0 an uncapped corroborated peak would reach.
    assert round(spike["score_breakdown"]["spike_factor"], 3) == 1.5
    assert "single sample of 6" in spike["summary"], spike["summary"]


def test_a_sustained_elevation_is_labelled_average_not_peak():
    """Negative control for the peak path: the original behaviour must be intact.

    `MAX >= AVG` holds by definition, so `peak_ratio >= ratio` is unconditionally true.
    A peak path that wins on a bare `>` therefore claims EVERY candidate, and a metric
    elevated flat across the window (avg 3.0x, peak 3.067x) gets reported and scored as
    a brief saturation. The two readings call for different actions, so the label has to
    follow the fact that actually qualified it.
    """
    cache = _dispatching_cache(
        window_max=_qr([
            {"metric_type": "cpu", "window_avg": 90.0, "baseline_avg": 30.0,
             "window_sum": 540.0, "baseline_sum": None, "window_max": 92.0,
             "window_samples": 6},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    spike = next(c for c in res["candidates"] if c["category"] == "metric_spike")
    assert spike["score_breakdown"]["qualified_by"] == "average", spike["score_breakdown"]
    assert spike["score_breakdown"]["lone_sample"] is False
    assert spike["evidence"]["ratio"] == 3.0
    # Scored off the average: 3.0 / 1.5 = 2.0, at the cap.
    assert round(spike["score_breakdown"]["spike_factor"], 3) == 2.0
    assert "spiked 3.0x" in spike["summary"], spike["summary"]
    assert "peaked" not in spike["summary"], spike["summary"]


def test_the_metric_spike_query_selects_what_the_scorer_reads():
    """The scorer reads window_sum and window_samples by name; SQL is their only source.

    Documented exception to this file's no-SQL-assertions rule. The fake cache returns
    rows without executing SQL, so a column that the query stops selecting still
    reaches the scorer as None from the test fixtures. In production that turns every
    peak candidate into an uncorroborated one and discounts them all by the same
    factor, so no ranking, score, or signals_examined assertion can tell the difference.
    """
    cache = _dispatching_cache(
        window_max=_qr([
            {"metric_type": "cpu", "window_avg": 66.667, "baseline_avg": 50.0,
             "window_sum": 400.0, "baseline_sum": None, "window_max": 100.0,
             "window_samples": 6},
        ]),
    )
    diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    spike_sql = next(
        c.args[0] for c in cache.execute.call_args_list
        if "window_max" in c.args[0] and "metric_snapshots" in c.args[0]
    )
    for column in ("window_max", "window_sum", "window_samples"):
        assert f"AS {column}" in spike_sql, f"{column} is read by the scorer but not selected"


def test_a_flat_metric_produces_no_spike_candidate():
    """Negative control: without this, a gate that admits everything passes both
    tests above."""
    cache = _dispatching_cache(
        window_max=_qr([
            {"metric_type": "cpu", "window_avg": 50.0, "baseline_avg": 50.0,
             "window_sum": 300.0, "baseline_sum": None, "window_max": 51.0,
             "window_samples": 6},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    assert res["signals_examined"]["metric_spikes"] == 0
    assert not [c for c in res["candidates"] if c["category"] == "metric_spike"]


def test_every_self_event_source_is_excluded_by_the_query_too():
    """The SQL predicate and the Python filter must name the SAME sources.

    They diverged the moment the set gained a second member: the predicate held one
    literal, `AND COALESCE(source, '') <> 'dbops-alert-evaluator'`, while the Python
    filter read the frozenset. The Python half hid it, so a scenario-runner row would
    have crossed the wire and been dropped only in-process, and a future reader
    trusting the query alone would have had it ranked.
    """
    cache = _dispatching_cache()
    diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    sql, params = next(
        (c.args[0], c.args[1] if len(c.args) > 1 else {})
        for c in cache.execute.call_args_list
        if "FROM event_log" in c.args[0]
    )
    bound = {v for k, v in (params or {}).items() if k.startswith("self_src_")}
    assert bound == set(SELF_EVENT_SOURCES), (
        f"query excludes {bound}, Python filter excludes {set(SELF_EVENT_SOURCES)}"
    )
    # And the predicate must actually reference those parameters, not just bind them.
    for i in range(len(SELF_EVENT_SOURCES)):
        assert f":self_src_{i}" in sql


def test_the_alert_that_triggered_the_rca_is_not_ranked_as_its_cause():
    """alert_evaluator writes its `alert` row into event_log BEFORE enqueuing the
    auto-RCA, so the row lands 1-2 seconds before the anchor where the recency factor
    peaks. Measured on the real 2026-08-30 auto-RCA: rank 1, score 3.6,
    "WARNING event 'alert' near the incident". The top-ranked cause was the alert that
    opened the investigation, and it outscored every genuine signal.
    """
    cache = _dispatching_cache(
        event_log=_qr([
            {"event_time": "2024-01-01T11:59:58Z", "event_type": "alert",
             "message": "PG high CPU: cpu = 100.00 > 80.0", "severity": "warning",
             "source": "dbops-alert-evaluator"},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    events = [c for c in res["candidates"] if c["category"] == "event"]
    assert events == [], f"the triggering alert must not be a candidate: {events}"
    assert res["signals_examined"]["events"] == 0


def test_a_real_engine_event_is_still_ranked():
    """Negative control for the source filter. proactive_monitor's anomaly_* rows and
    aws.rds native events are genuine evidence; only the alert emitter is circular.
    A filter that dropped everything would pass the test above and blind the tool.
    """
    cache = _dispatching_cache(
        event_log=_qr([
            {"event_time": "2024-01-01T11:58:00Z", "event_type": "failover",
             "message": "Aurora failover completed", "severity": "critical",
             "source": "aws.rds"},
            {"event_time": "2024-01-01T11:57:00Z", "event_type": "anomaly_freeable_memory",
             "message": "freeable_memory 4.2 sigma low", "severity": "critical",
             "source": "dbops-monitor"},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    sources = {c["evidence"].get("source") for c in res["candidates"] if c["category"] == "event"}
    assert sources == {"aws.rds", "dbops-monitor"}, sources
    assert res["signals_examined"]["events"] == 2


# ---------------------------------------------------------------------------
# slow_queries: one row per query, costed by the window
# ---------------------------------------------------------------------------

def test_a_slow_query_is_costed_by_the_window_not_its_lifetime_total():
    """total_time_ms is CUMULATIVE since the last stats reset.

    pg_stat_statements and performance_schema both accumulate, so the number the
    report used to show was a lifetime total presented as this incident's cost.
    Measured on the real cluster: cumulative 4,959,817ms against a 60-minute window
    delta of 15,861ms, an overstatement of 313x. A query that has run quietly for a
    month then outranks one that started melting the database ten minutes ago.

    Here: 900,000ms cumulative, 880,000ms of it already spent before the window, so
    the window cost is 20,000ms and the summary must say 20,000, not 900,000.
    """
    cache = _dispatching_cache(
        query_stats=_qr([
            {"query_hash": "h1", "query_text": "SELECT 1",
             "win_max": 900000.0, "win_min": 884000.0, "pre_max": 880000.0,
             "calls_max": 5000, "calls_min": 4900, "pre_calls": 4800,
             "mean_time_ms": 180.0, "snapshot_time": "2024-01-01T11:58:00Z",
             "snapshots": 6},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    sq = next(c for c in res["candidates"] if c["category"] == "slow_query")
    assert sq["evidence"]["window_time_ms"] == 20000.0, sq["evidence"]
    assert sq["evidence"]["cumulative_time_ms"] == 900000.0
    assert sq["evidence"]["window_calls"] == 200
    assert sq["evidence"]["first_seen_in_window"] is False
    assert "20000.0ms in window" in sq["summary"], sq["summary"]
    assert "900000" not in sq["summary"], (
        "the lifetime total is in the summary, which is the overstatement itself"
    )


def test_a_query_first_seen_in_the_window_is_costed_by_its_whole_reading():
    """No pre-window reading means the query did not exist before, so its cumulative
    value IS its in-window cost.

    This branch is what keeps a brand new heavy query rankable. The obvious dedupe
    (GROUP BY with HAVING COUNT(*) >= 2, so a delta can be taken) would drop exactly
    the query that appeared during the incident, which is the one most worth seeing.
    """
    cache = _dispatching_cache(
        query_stats=_qr([
            {"query_hash": "new1", "query_text": "SELECT pg_sleep(9)",
             "win_max": 41000.0, "win_min": 41000.0, "pre_max": None,
             "calls_max": 12, "calls_min": 12, "pre_calls": None,
             "mean_time_ms": 3416.0, "snapshot_time": "2024-01-01T11:59:00Z",
             "snapshots": 1},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    sq = next(c for c in res["candidates"] if c["category"] == "slow_query")
    assert sq["evidence"]["window_time_ms"] == 41000.0
    assert sq["evidence"]["window_calls"] == 12
    assert sq["evidence"]["first_seen_in_window"] is True
    assert sq["evidence"]["snapshots_in_window"] == 1


def test_a_stats_reset_mid_window_does_not_produce_a_negative_cost():
    """MIN can come from before a reset and MAX from after it, so the delta goes
    negative. A negative cost sorts to the bottom and reads as nonsense; clamped to
    zero it simply carries no weight, which is the honest answer for a counter whose
    history was wiped."""
    cache = _dispatching_cache(
        query_stats=_qr([
            {"query_hash": "r1", "query_text": "SELECT 2",
             "win_max": 50.0, "win_min": 10.0, "pre_max": 900000.0,
             "calls_max": 3, "calls_min": 1, "pre_calls": 8000,
             "mean_time_ms": 16.0, "snapshot_time": "2024-01-01T11:55:00Z",
             "snapshots": 4},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    sq = next(c for c in res["candidates"] if c["category"] == "slow_query")
    assert sq["evidence"]["window_time_ms"] == 0.0
    assert sq["evidence"]["window_calls"] == 0


def test_the_slow_query_statement_groups_by_hash_and_reads_the_baseline():
    """Documented exception to this file's no-SQL-assertions rule, same reason as the
    metric one: the fake never executes SQL.

    Without the GROUP BY, the collector takes the top 3 ROWS, and the collector writes
    one row per query per cycle. Measured on the real 2026-08-30 auto-RCA: candidates
    2, 3 and 4 were the same query at 616380.7ms, 616380.7ms and 616254.2ms. One query
    filled three of the four slots in the whole report, displacing the other evidence.
    Without the baseline half there is nothing to take a delta FROM.
    """
    cache = _dispatching_cache()
    diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    sql, params = next(
        (c.args[0], c.args[1] if len(c.args) > 1 else {})
        for c in cache.execute.call_args_list
        if "FROM query_stats" in c.args[0]
    )
    assert "GROUP BY query_hash" in sql, "the statement can still return one query three times"
    assert "baseline_start" in params, "no pre-window reading to measure a delta from"
    for column in ("win_max", "pre_max", "calls_max", "pre_calls", "snapshots"):
        assert f"AS {column}" in sql, f"{column} is read by the scorer but not selected"
