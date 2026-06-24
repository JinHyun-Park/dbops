"""Tests for diagnose_root_cause_impl.

The impl issues several cache.execute calls IN THIS ORDER when ``around_time``
is supplied (passing around_time skips the NOW() probe, keeping the order
deterministic):

    1. schema_snapshots  (schema/DDL changes)
    2. event_log         (operational events)
    3. blocking_locks    (lock contention)
    4. metric_snapshots  (metric spikes, grouped per metric_type)
    5. query_stats       (top slow queries)

So ``cache.execute.side_effect`` is a 5-element list matching that order.
Assertions target the RETURN structure (ranks/categories/scores/
signals_examined), never the SQL strings, so they survive SQL tweaks.
"""

from unittest.mock import MagicMock

from mcp_servers.incident.tools.diagnose_root_cause import diagnose_root_cause_impl
from mcp_servers.shared.models import QueryResult

ANCHOR = "2024-01-01T12:00:00Z"


def _qr(rows):
    return QueryResult(columns=list(rows[0].keys()) if rows else [], rows=rows, row_count=len(rows))


def _empty():
    return QueryResult(columns=[], rows=[], row_count=0)


def test_ranks_schema_change_event_and_metric_spike():
    cache = MagicMock()
    # Order: schema_changes, events, blocking, metric_spikes, slow_queries
    cache.execute.side_effect = [
        # schema change right at the anchor -> should rank at/near the top
        _qr([
            {
                "snapshot_time": "2024-01-01T11:59:00Z",
                "schema_name": "public",
                "changes": '{"added_index": "idx_orders_status"}',
            }
        ]),
        # a critical event a couple minutes before the anchor
        _qr([
            {
                "event_time": "2024-01-01T11:58:00Z",
                "event_type": "failover",
                "message": "Writer failover started",
                "severity": "critical",
                "source": "rds-event",
            }
        ]),
        # no blocking locks
        _empty(),
        # cpu spiked 3x vs baseline
        _qr([
            {"metric_type": "cpu", "window_avg": 90.0, "baseline_avg": 30.0},
            {"metric_type": "connections", "window_avg": 50.0, "baseline_avg": 48.0},  # not a spike
        ]),
        # one slow query
        _qr([
            {
                "query_hash": "abc123",
                "query_text": "SELECT * FROM orders WHERE status = $1",
                "calls": 1200,
                "total_time_ms": 54000.0,
                "mean_time_ms": 45.0,
                "snapshot_time": "2024-01-01T11:57:00Z",
            }
        ]),
    ]

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
        "slow_queries": 1,
        "elasticache_signals": 0,
    }


def test_empty_cache_returns_no_candidates():
    cache = MagicMock()
    cache.execute.side_effect = [_empty(), _empty(), _empty(), _empty(), _empty()]

    result = diagnose_root_cause_impl(cache, cluster_id="prod-pg-1", around_time=ANCHOR)

    assert result["candidates"] == []
    assert "correlation, not proof" in result["note"]
    assert result["signals_examined"] == {
        "schema_changes": 0,
        "events": 0,
        "blocking": 0,
        "metric_spikes": 0,
        "slow_queries": 0,
        "elasticache_signals": 0,
    }


def test_missing_table_on_one_source_still_ranks_others():
    cache = MagicMock()
    # schema_snapshots table is absent -> first execute raises; the rest succeed.
    cache.execute.side_effect = [
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
    assert "schema_changes" in out["skipped_sources"]
    assert out["signals_examined"]["schema_changes"] == 0
