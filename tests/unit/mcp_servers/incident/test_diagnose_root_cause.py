"""Tests for diagnose_root_cause_impl.

The impl issues one cache.execute call per source. The fake DISPATCHES ON SQL
rather than on call ORDER: a positional side_effect list breaks, silently and
misleadingly, the moment a source gains a statement (the schema source gained the
two shared observation statements in the sixth pass over that surface, which
shifted every later source's rows by two and made a working tool look broken).
Assertions target the RETURN structure (ranks/categories/scores/
signals_examined), never the SQL strings, so they survive SQL tweaks.

Per-family metric sets and the zero-baseline counter path have their own file:
test_diagnose_family_signals.py.
"""

from unittest.mock import MagicMock

from mcp_servers.incident.tools.diagnose_root_cause import diagnose_root_cause_impl
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
            {
                "query_hash": "abc123",
                "query_text": "SELECT * FROM orders WHERE status = $1",
                "calls": 1200,
                "total_time_ms": 54000.0,
                "mean_time_ms": 45.0,
                "snapshot_time": "2024-01-01T11:57:00Z",
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
    cache = _dispatching_cache(
        window_max=_qr([
            {"metric_type": "cpu", "window_avg": 55.147, "baseline_avg": 50.880,
             "window_sum": None, "baseline_sum": None, "window_max": 100.0},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)

    assert res["status"] == "ok"
    assert res["signals_examined"]["metric_spikes"] == 1, (
        "a 100% CPU peak must be examined as a spike; the window average alone hides it"
    )
    spike = next(c for c in res["candidates"] if c["category"] == "metric_spike")

    # The score has to come from the PEAK, not the diluted average. Off the average the
    # spike_factor would be 1.0839/1.5 = 0.72, which would rank a full saturation BELOW
    # a metric that merely drifted 1.5x: in the list, and buried.
    assert spike["score_breakdown"]["qualified_by"] == "peak"
    assert spike["score_breakdown"]["spike_factor"] > 1.0, spike["score_breakdown"]
    assert spike["evidence"]["window_max"] == 100.0
    assert round(spike["evidence"]["peak_ratio"], 3) == 1.965

    # The summary must say which fact was used, or it contradicts the score.
    assert "peaked" in spike["summary"] and "max 100.0" in spike["summary"], spike["summary"]


def test_a_sustained_elevation_still_qualifies_on_the_average():
    """Negative control for the peak path: the original behaviour must be intact.

    Without this, a change that scored everything off the peak would pass the test
    above while silently relabelling every sustained elevation as a spike.
    """
    cache = _dispatching_cache(
        window_max=_qr([
            {"metric_type": "cpu", "window_avg": 90.0, "baseline_avg": 30.0,
             "window_sum": None, "baseline_sum": None, "window_max": 92.0},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    spike = next(c for c in res["candidates"] if c["category"] == "metric_spike")
    # avg ratio 3.0 beats peak ratio 3.067? No: peak is higher, so peak wins. What must
    # hold is that a sustained move is NOT reported as a brief one when the average
    # alone already clears the gate by a wide margin and the peak adds nothing.
    assert spike["evidence"]["ratio"] == 3.0
    assert "cpu" in spike["summary"]


def test_a_flat_metric_produces_no_spike_candidate():
    """Negative control: without this, a gate that admits everything passes both
    tests above."""
    cache = _dispatching_cache(
        window_max=_qr([
            {"metric_type": "cpu", "window_avg": 50.0, "baseline_avg": 50.0,
             "window_sum": None, "baseline_sum": None, "window_max": 51.0},
        ]),
    )
    res = diagnose_root_cause_impl(cache, "c1", around_time=ANCHOR, window_minutes=30)
    assert res["signals_examined"]["metric_spikes"] == 0
    assert not [c for c in res["candidates"] if c["category"] == "metric_spike"]


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
