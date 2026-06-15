"""Tests for dashboard latency/throttle hardening:

  1. _overview — the four cache-DB reads now fan out across a thread pool
     instead of running serially. Result SHAPE and the cold-resource registry
     fallback must be byte-for-byte unchanged; all four queries must still run.
  2. _cached_live — warm-container TTL cache that fronts the expensive
     cross-account live-describe endpoints (topology/backups/engine-config).
     Bounds the rds:Describe* / cloudwatch:GetMetric* / sts:AssumeRole call
     rate so concurrent dashboard pollers don't exhaust region-level quotas.
"""

import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading — mirror test_dashboard_engine_gating.py so engine_family
# resolves and module-import env vars are satisfied.
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler_perf", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


@pytest.fixture(autouse=True)
def _clear_live_cache():
    """The live-describe cache is module-level (warm-container semantics in
    prod). Clear it before each test so cases don't leak cached entries."""
    handler._LIVE_CACHE.clear()
    yield
    handler._LIVE_CACHE.clear()


# ===========================================================================
# 1. _overview — parallel fan-out preserves structure + runs all four reads
# ===========================================================================

def test_overview_parallel_runs_all_four_reads_and_preserves_shape():
    """All four cache-DB reads must execute (one per table) and the returned
    dict must keep the exact {cluster, metrics, top_queries, events} shape the
    frontend depends on — regardless of the now-concurrent execution."""
    seen = []
    lock = threading.Lock()

    metrics_row = {"metric_type": "cpu", "avg_val": 12.0, "max_val": 30.0}
    query_row = {"query_hash": "h1", "query_text": "SELECT 1", "calls": 5,
                 "total_time_ms": 100.0, "mean_time_ms": 20.0}
    event_row = {"id": "e1", "ts": "2026-06-15T00:00:00Z", "event_type": "deploy",
                 "severity": "info", "source": "rds", "message": "x", "raw_event": "{}"}
    meta_row = {"cluster_id": "prod-pg", "engine": "aurora-postgresql", "status": "available"}

    def _spy_query(sql, params=None):
        with lock:
            seen.append(sql)
        if "cluster_meta" in sql:
            return [meta_row]
        if "metric_snapshots" in sql:
            return [metrics_row]
        if "query_stats" in sql:
            return [query_row]
        if "event_log" in sql:
            return [event_row]
        return []

    result = handler._overview(_spy_query, "prod-pg")

    # All four table reads happened.
    joined = " ".join(seen)
    assert "cluster_meta" in joined
    assert "metric_snapshots" in joined
    assert "query_stats" in joined
    assert "event_log" in joined
    assert len(seen) == 4

    # Shape + values preserved exactly.
    assert result["cluster"] == meta_row
    assert result["metrics"] == [metrics_row]
    assert result["top_queries"] == [query_row]
    assert result["events"] == [event_row]


def test_overview_parallel_cold_resource_registry_fallback_intact(monkeypatch):
    """When cluster_meta has no row, the post-batch registry fallback must
    still synthesise the engine stub (unchanged behaviour from the serial
    version) so the frontend gates on engine_family correctly."""
    monkeypatch.setattr(handler, "_lookup_cluster",
                        lambda cid: {"cluster_id": cid, "engine": "dynamodb"})

    def _empty_query(sql, params=None):
        return []

    result = handler._overview(_empty_query, "ddb-cold-1")

    cluster = result["cluster"]
    assert cluster is not None
    assert cluster["engine"] == "dynamodb"
    assert cluster["engine_family"] == "dynamodb"
    assert cluster["cluster_id"] == "ddb-cold-1"
    assert result["metrics"] == []
    assert result["top_queries"] == []
    assert result["events"] == []


def test_overview_parallel_no_registry_returns_none(monkeypatch):
    """No cluster_meta row AND empty registry → cluster=None (unchanged)."""
    monkeypatch.setattr(handler, "_lookup_cluster", lambda cid: {})

    result = handler._overview(lambda sql, params=None: [], "ghost")
    assert result["cluster"] is None


def test_overview_parallel_propagates_query_error():
    """If a read raises, the future surfaces it (we do NOT silently swallow a
    cache-DB failure into a half-populated overview)."""
    def _boom(sql, params=None):
        if "event_log" in sql:
            raise RuntimeError("data api blew up")
        return []

    with pytest.raises(RuntimeError, match="data api blew up"):
        handler._overview(_boom, "prod-pg")


# ===========================================================================
# 2. _cached_live — warm-container TTL cache
# ===========================================================================

class _Clock:
    """Controllable monotonic clock so TTL behaviour is tested without sleep."""
    def __init__(self, t0=1000.0):
        self.t = t0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_cached_live_serves_from_cache_within_ttl(monkeypatch):
    """Producer runs once; a second call within the TTL is served from cache
    (the producer is NOT re-invoked) — this is what caps the live-AWS rate."""
    clock = _Clock()
    monkeypatch.setattr(handler.time, "monotonic", clock)

    calls = {"n": 0}

    def _producer():
        calls["n"] += 1
        return {"members": [calls["n"]]}

    first = handler._cached_live("topology:c1", 25, _producer)
    clock.advance(10)  # still within 25s TTL
    second = handler._cached_live("topology:c1", 25, _producer)

    assert calls["n"] == 1, "producer must run exactly once within the TTL"
    assert first == {"members": [1]}
    assert second == first  # identical cached object


def test_cached_live_refreshes_after_ttl(monkeypatch):
    """Once the TTL lapses, the next call re-invokes the producer."""
    clock = _Clock()
    monkeypatch.setattr(handler.time, "monotonic", clock)

    calls = {"n": 0}

    def _producer():
        calls["n"] += 1
        return {"v": calls["n"]}

    handler._cached_live("backups:c1", 55, _producer)
    clock.advance(56)  # past the 55s TTL
    second = handler._cached_live("backups:c1", 55, _producer)

    assert calls["n"] == 2
    assert second == {"v": 2}


def test_cached_live_keys_are_isolated(monkeypatch):
    """Different keys (different cluster/endpoint) don't collide."""
    clock = _Clock()
    monkeypatch.setattr(handler.time, "monotonic", clock)

    handler._cached_live("topology:a", 25, lambda: {"who": "a"})
    b = handler._cached_live("topology:b", 25, lambda: {"who": "b"})
    a = handler._cached_live("topology:a", 25, lambda: {"who": "SHOULD_NOT_RUN"})

    assert a == {"who": "a"}
    assert b == {"who": "b"}


def test_cached_live_error_uses_short_negative_ttl(monkeypatch):
    """An error-shaped result (dict with truthy 'error') is cached only for the
    short negative TTL, so a transient describe failure doesn't pin a panel for
    the full minute — but is still throttled below every-poll."""
    clock = _Clock()
    monkeypatch.setattr(handler.time, "monotonic", clock)

    calls = {"n": 0}

    def _producer():
        calls["n"] += 1
        return {"error": "describe failed", "members": []}

    handler._cached_live("topology:err", 25, _producer)

    # Within the negative TTL (5s) → still cached.
    clock.advance(3)
    handler._cached_live("topology:err", 25, _producer)
    assert calls["n"] == 1

    # Past the negative TTL (but well within the 25s success TTL) → re-run.
    clock.advance(3)  # total 6s > 5s negative TTL
    handler._cached_live("topology:err", 25, _producer)
    assert calls["n"] == 2, "error result must expire at the short negative TTL, not the long one"


def test_cached_live_caches_producer_exception_and_throttles(monkeypatch):
    """When producer() RAISES (e.g. sts:AssumeRole / rds:Describe* throttle),
    the failure is cached for the negative TTL and re-raised on hits — so a
    hard-failing cluster isn't retried live on every poll (the thundering-herd
    the cache exists to prevent). The exception still propagates (preserving the
    routing-layer 500), it's just throttled."""
    clock = _Clock()
    monkeypatch.setattr(handler.time, "monotonic", clock)

    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise RuntimeError("assume_role throttled")

    # First call: producer raises → propagates.
    with pytest.raises(RuntimeError, match="assume_role throttled"):
        handler._cached_live("topology:x", 25, _boom)

    # Within the negative TTL: cached exception re-raised, producer NOT re-run.
    clock.advance(3)
    with pytest.raises(RuntimeError, match="assume_role throttled"):
        handler._cached_live("topology:x", 25, _boom)
    assert calls["n"] == 1, "producer must not be re-invoked within the negative TTL"

    # Past the negative TTL: producer runs again.
    clock.advance(3)  # total 6s > 5s negative TTL
    with pytest.raises(RuntimeError):
        handler._cached_live("topology:x", 25, _boom)
    assert calls["n"] == 2


def test_cached_live_negative_ttl_measured_after_producer(monkeypatch):
    """The entry timestamp is taken AFTER producer() returns, so a slow failing
    call still gets its full negative TTL (it must not land already-expired)."""
    clock = _Clock()
    monkeypatch.setattr(handler.time, "monotonic", clock)

    calls = {"n": 0}

    def _slow_error():
        calls["n"] += 1
        clock.advance(10)  # producer itself takes 10s
        return {"error": "describe slow-failed"}

    handler._cached_live("backups:slow", 55, _slow_error)
    # 2s after the producer returned — well inside the 5s negative TTL even
    # though the call itself took 10s. If the timestamp were captured BEFORE the
    # call, the entry would already be expired and the producer would re-run.
    clock.advance(2)
    handler._cached_live("backups:slow", 55, _slow_error)
    assert calls["n"] == 1, "negative TTL must be measured from when producer finished, not started"


def test_cached_live_size_cap_drops_cache_on_overflow(monkeypatch):
    """A pathological key explosion can't pin a warm container's memory — once
    the cache hits _LIVE_CACHE_MAX, inserting a new key clears it first."""
    clock = _Clock()
    monkeypatch.setattr(handler.time, "monotonic", clock)
    monkeypatch.setattr(handler, "_LIVE_CACHE_MAX", 4)

    for i in range(4):
        handler._cached_live(f"topology:k{i}", 25, lambda i=i: {"v": i})
    assert len(handler._LIVE_CACHE) == 4

    # 5th distinct key → over the cap → clear, then store just the new one.
    handler._cached_live("topology:k_new", 25, lambda: {"v": "new"})
    assert len(handler._LIVE_CACHE) == 1
    assert handler._LIVE_CACHE["topology:k_new"][1] == {"v": "new"}


def test_cached_live_success_after_error_gets_full_ttl(monkeypatch):
    """A success following a cached error gets the FULL ttl (not the negative
    one) — the negative TTL must not stick to the key permanently."""
    clock = _Clock()
    monkeypatch.setattr(handler.time, "monotonic", clock)

    state = {"fail": True, "n": 0}

    def _producer():
        state["n"] += 1
        if state["fail"]:
            return {"error": "down"}
        return {"members": ["ok"]}

    handler._cached_live("topology:flap", 25, _producer)  # error, neg TTL
    clock.advance(6)  # neg TTL lapsed
    state["fail"] = False
    handler._cached_live("topology:flap", 25, _producer)  # success, full TTL
    clock.advance(10)  # within 25s but > 5s
    result = handler._cached_live("topology:flap", 25, _producer)

    assert state["n"] == 2, "success must be cached for the full 25s TTL, not re-run after 10s"
    assert result == {"members": ["ok"]}
