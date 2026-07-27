"""E1-2: diagnose_root_cause must rank metric signals for EVERY engine family,
including counters whose baseline is legitimately zero.

Before this, the metric query hardcoded ('aas','cpu','db_connections'), which
are Aurora's names, so DocumentDB (cpu_utilization), ElastiCache
(cache_cpu/engine_cpu), DynamoDB (throttle/consumed) and standalone RDS
instances ranked ZERO metric signals: the tool answered with nothing in it.

Each family test seeds a realistic incident and asserts the signal is RANKED
(carries a rank, and outranks a competing candidate), not merely present.

The cache answers are dispatched on the SQL text so these tests do not depend on
the order of the impl's queries.
"""

import re
from unittest.mock import MagicMock

from mcp_servers.incident.tools.diagnose_root_cause import diagnose_root_cause_impl
from mcp_servers.shared.models import QueryResult

ANCHOR = "2026-07-24T12:00:00Z"
# An informational event 2 minutes before the anchor: a real competing candidate
# (score ~1.9) so "ranked" means "ranked above something", not "alone in a list".
COMPETING_EVENT = {
    "event_time": "2026-07-24T11:58:00Z",
    "event_type": "housekeeping",
    "message": "routine maintenance window opened",
    "severity": "info",
    "source": "rds-event",
}
_M_PARAM = re.compile(r"^m\d+$")


def _qr(rows):
    return QueryResult(columns=list(rows[0].keys()) if rows else [], rows=rows, row_count=len(rows))


def _cache(engine, grouped=None, elasticache=None, events=None):
    cache = MagicMock()

    def _side(sql, params=None):
        if "cluster_meta" in sql:
            return _qr([{"engine": engine}]) if engine else _qr([])
        if "GROUP BY metric_type" in sql:
            return _qr(grouped or [])
        if "'evictions'" in sql:  # _collect_elasticache_signals
            return _qr(elasticache or [])
        if "event_log" in sql:
            return _qr(events or [])
        return _qr([])

    cache.execute.side_effect = _side
    return cache


def _searched_metrics(cache):
    """The metric_type names actually bound into the grouped metric query."""
    for call in cache.execute.call_args_list:
        sql = call[0][0]
        if "GROUP BY metric_type" in sql:
            params = call[0][1]
            return sorted(v for k, v in params.items() if _M_PARAM.match(k))
    raise AssertionError("the grouped metric query was never issued")


def _row(metric_type, window_avg, baseline_avg, window_sum, baseline_sum):
    return {
        "metric_type": metric_type,
        "window_avg": window_avg,
        "baseline_avg": baseline_avg,
        "window_sum": window_sum,
        "baseline_sum": baseline_sum,
    }


def _run(cache):
    return diagnose_root_cause_impl(cache, cluster_id="c1", around_time=ANCHOR, window_minutes=30)


# --- regression guard: Aurora / relational must be untouched -----------------


def test_relational_signal_set_and_ranking_unchanged():
    """Same three series, same score. This is the invariant E1-2 must not move."""
    cache = _cache(
        "aurora-postgresql",
        grouped=[
            _row("cpu", 90.0, 30.0, 2700.0, 900.0),          # 3x -> spike
            _row("db_connections", 50.0, 48.0, 1500.0, 1440.0),  # 1.04x -> not a spike
        ],
    )
    out = _run(cache)

    assert out["engine_family"] == "relational"
    assert _searched_metrics(cache) == ["aas", "cpu", "db_connections"]

    cands = out["candidates"]
    assert [c["category"] for c in cands] == ["metric_spike"]
    assert cands[0]["evidence"]["metric_type"] == "cpu"
    # base 2.0 x recency 0.65 (window midpoint) x spike 2.0 (capped) = 2.6,
    # the exact score this tool produced before the per-family split.
    assert cands[0]["score"] == 2.6
    assert out["signals_examined"]["counter_spikes"] == 0
    assert not any(c["category"] == "counter_spike" for c in cands)


def test_relational_has_no_counter_path():
    """Aurora writes `deadlocks` as a Sum metric; it must NOT start producing
    counter candidates, or relational answers would change."""
    cache = _cache(
        "aurora-mysql",
        grouped=[_row("deadlocks", 3.0, 0.0, 90.0, 0.0)],
    )
    out = _run(cache)
    assert "deadlocks" not in _searched_metrics(cache)
    assert out["candidates"] == []


# --- DynamoDB: throttle spike off a zero baseline ---------------------------


def test_dynamodb_throttle_spike_from_zero_baseline_is_ranked():
    cache = _cache(
        "dynamodb",
        grouped=[
            _row("consumed_rcu", 300.0, 280.0, 9000.0, 8400.0),   # normal load
            _row("write_throttle_events", 4.7, 0.0, 142.0, 0.0),  # the incident
        ],
        events=[COMPETING_EVENT],
    )
    out = _run(cache)

    assert out["engine_family"] == "dynamodb"
    searched = _searched_metrics(cache)
    assert "write_throttle_events" in searched
    assert "cpu" not in searched  # DynamoDB has no CPU series at all

    throttle = next(c for c in out["candidates"] if c["evidence"]["metric_type"] == "write_throttle_events")
    assert throttle["category"] == "counter_spike"
    assert throttle["rank"] == 1  # RANKED, and above the competing event
    assert throttle["evidence"]["from_zero_baseline"] is True
    assert throttle["evidence"]["window_total"] == 142.0
    assert out["signals_examined"]["counter_spikes"] == 1
    # magnitude never divides by the zero baseline: it divides by the noise floor
    assert throttle["score_breakdown"]["noise_floor"] == 1.0
    assert throttle["score_breakdown"]["magnitude_factor"] == 2.0


def test_flat_zero_counter_produces_no_signal():
    """Control: the counter path must not degrade into an always-firing alarm.
    A healthy table reports 0 throttles in both windows."""
    cache = _cache(
        "dynamodb",
        grouped=[
            _row("read_throttle_events", 0.0, 0.0, 0.0, 0.0),
            _row("write_throttle_events", 0.0, 0.0, 0.0, 0.0),
        ],
    )
    out = _run(cache)
    assert out["candidates"] == []
    assert out["signals_examined"]["counter_spikes"] == 0


def test_steady_nonzero_counter_needs_a_real_jump():
    """A counter that is always somewhat nonzero must not fire every window,
    but a genuine multiple of its usual level must."""
    steady = _cache("dynamodb", grouped=[_row("read_throttle_events", 3.6, 3.3, 110.0, 100.0)])
    assert _run(steady)["candidates"] == []

    jumped = _cache("dynamodb", grouped=[_row("read_throttle_events", 10.0, 3.3, 300.0, 100.0)])
    cands = _run(jumped)["candidates"]
    assert len(cands) == 1
    assert cands[0]["category"] == "counter_spike"
    assert cands[0]["evidence"]["from_zero_baseline"] is False


# --- DocumentDB: CPU climb under its own metric name ------------------------


def test_documentdb_cpu_climb_is_ranked():
    cache = _cache(
        "docdb",
        grouped=[_row("cpu_utilization", 82.0, 35.0, 2460.0, 1050.0)],
        events=[COMPETING_EVENT],
    )
    out = _run(cache)

    assert out["engine_family"] == "documentdb"
    searched = _searched_metrics(cache)
    assert "cpu_utilization" in searched  # not 'cpu', the name that was dead
    assert "cursors_timed_out" in searched

    spike = next(c for c in out["candidates"] if c["category"] == "metric_spike")
    assert spike["evidence"]["metric_type"] == "cpu_utilization"
    assert spike["rank"] == 1


def test_documentdb_cursor_timeout_counter_is_ranked():
    cache = _cache("docdb", grouped=[_row("cursors_timed_out", 0.4, 0.0, 12.0, 0.0)])
    cands = _run(cache)["candidates"]
    assert len(cands) == 1
    assert cands[0]["category"] == "counter_spike"
    assert cands[0]["evidence"]["metric_type"] == "cursors_timed_out"
    assert cands[0]["rank"] == 1


# --- ElastiCache: eviction storm + engine CPU ------------------------------


def test_elasticache_eviction_storm_and_cpu_are_ranked():
    """The eviction storm ranks through _collect_elasticache_signals (per-datapoint
    evidence, which is why evictions are deliberately not in the counter table),
    and the cache's own CPU series now ranks too."""
    cache = _cache(
        "redis",
        grouped=[_row("engine_cpu", 95.0, 40.0, 2850.0, 1200.0)],
        elasticache=[
            {"ts": "2026-07-24T11:55:00Z", "metric_type": "evictions", "value": 4200},
        ],
        events=[COMPETING_EVENT],
    )
    out = _run(cache)

    assert out["engine_family"] == "elasticache"
    searched = _searched_metrics(cache)
    assert "engine_cpu" in searched and "cache_cpu" in searched
    # not listed twice: the elasticache collector owns these two
    assert "evictions" not in searched and "replication_lag" not in searched

    cats = [c["category"] for c in out["candidates"]]
    assert "elasticache_spike" in cats
    assert "metric_spike" in cats
    ranks = {c["category"]: c["rank"] for c in out["candidates"]}
    assert ranks["elasticache_spike"] < ranks.get("event", 99)
    cpu = next(c for c in out["candidates"] if c["category"] == "metric_spike")
    assert cpu["evidence"]["metric_type"] == "engine_cpu"


# --- standalone RDS instance ----------------------------------------------


def test_rds_instance_connection_surge_is_ranked():
    cache = _cache(
        "sqlserver-se",
        grouped=[
            _row("db_connections", 480.0, 120.0, 14400.0, 3600.0),
            _row("cpu", 44.0, 40.0, 1320.0, 1200.0),  # not a spike
        ],
        events=[COMPETING_EVENT],
    )
    out = _run(cache)

    assert out["engine_family"] == "rds_instance"
    searched = _searched_metrics(cache)
    # Aurora-only names must NOT be searched here: CloudWatch publishes neither
    # for a standalone instance.
    assert "replica_lag_ms" not in searched and "buffer_cache_hit" not in searched
    assert "read_latency" in searched and "aas" in searched

    surge = next(c for c in out["candidates"] if c["category"] == "metric_spike")
    assert surge["evidence"]["metric_type"] == "db_connections"
    assert surge["rank"] == 1


# --- honesty about an unresolved family -----------------------------------


def test_unregistered_cluster_reports_unknown_family():
    """cluster_meta miss -> the metric set is a guess, so say so rather than
    reporting a silent zero (that silence is the bug this task fixes)."""
    cache = _cache(None, grouped=[])
    out = _run(cache)
    assert out["status"] == "ok"
    assert out["engine_family"] == "unknown"
    assert "engine_family" in out["skipped_sources"]
