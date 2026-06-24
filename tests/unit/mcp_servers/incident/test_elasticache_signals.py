"""Tests for _collect_elasticache_signals in diagnose_root_cause.

Mirrors the import style of test_diagnose_root_cause.py (direct package import).

Note: _collect_* functions receive anchor as a datetime object (set by
_resolve_anchor inside diagnose_root_cause_impl). Tests that call these
functions directly must pass a datetime, not a string.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from mcp_servers.incident.tools.diagnose_root_cause import (
    BASE_WEIGHTS,
    _collect_elasticache_signals,
)

# Anchor as a proper datetime (matching how diagnose_root_cause_impl calls collectors)
_ANCHOR = datetime(2026, 6, 24, 0, 8, 0, tzinfo=timezone.utc)


class _Res:
    def __init__(self, rows):
        self.rows = rows


def test_elasticache_signals_eviction_and_lag():
    cache = MagicMock()
    cache.execute.return_value = _Res([
        {"ts": "2026-06-24T00:05:00Z", "metric_type": "evictions", "value": 500},
        {"ts": "2026-06-24T00:06:00Z", "metric_type": "replication_lag", "value": 1500},
    ])
    examined, skipped = {}, []
    out = _collect_elasticache_signals(
        cache, "my-redis", "2026-06-24T00:00:00Z", "2026-06-24T00:10:00Z",
        _ANCHOR, 10, examined, skipped)
    cats = {c["category"] for c in out}
    assert "elasticache_spike" in cats
    assert all("score" in c and "when" in c for c in out)


def test_elasticache_signals_skips_on_error():
    cache = MagicMock()
    cache.execute.side_effect = Exception("no table")
    examined, skipped = {}, []
    out = _collect_elasticache_signals(
        cache, "x", "2026-06-24T00:00:00Z", "2026-06-24T00:10:00Z",
        _ANCHOR, 10, examined, skipped)
    assert out == [] and "elasticache_signals" in skipped


def test_base_weight_present():
    assert BASE_WEIGHTS.get("elasticache_spike") == 2.5


def test_candidate_fields_complete():
    """Every candidate must carry all required candidate dict fields."""
    cache = MagicMock()
    cache.execute.return_value = _Res([
        {"ts": "2026-06-24T00:05:00Z", "metric_type": "evictions", "value": 200},
    ])
    examined, skipped = {}, []
    out = _collect_elasticache_signals(
        cache, "my-redis", "2026-06-24T00:00:00Z", "2026-06-24T00:10:00Z",
        _ANCHOR, 10, examined, skipped)
    assert len(out) == 1
    c = out[0]
    for field in ("category", "score", "score_breakdown", "summary", "evidence", "when", "suggested_action"):
        assert field in c, f"missing field: {field}"
    assert c["score"] > 0
    assert c["score_breakdown"]["base_weight"] == 2.5
    assert "recency_factor" in c["score_breakdown"]
    assert "formula" in c["score_breakdown"]


def test_replication_lag_candidate():
    """Replication-lag rows produce a distinct summary and action."""
    cache = MagicMock()
    cache.execute.return_value = _Res([
        {"ts": "2026-06-24T00:05:00Z", "metric_type": "replication_lag", "value": 2000},
    ])
    examined, skipped = {}, []
    out = _collect_elasticache_signals(
        cache, "my-redis", "2026-06-24T00:00:00Z", "2026-06-24T00:10:00Z",
        _ANCHOR, 10, examined, skipped)
    assert len(out) == 1
    assert "Replication Lag" in out[0]["summary"]
    assert "replication" in out[0]["suggested_action"].lower()


def test_empty_rows_returns_empty_list():
    cache = MagicMock()
    cache.execute.return_value = _Res([])
    examined, skipped = {}, []
    out = _collect_elasticache_signals(
        cache, "aurora-cluster", "2026-06-24T00:00:00Z", "2026-06-24T00:10:00Z",
        _ANCHOR, 10, examined, skipped)
    assert out == []
    assert examined.get("elasticache_signals") == 0
