"""DocumentDB findings collector — TDD test suite.

Strategy: patch _execute at the module level so every SQL call is intercepted.
Branch on SQL keywords to inject fake cache rows (metric_snapshots aggregates),
capture INSERTs, assert emitted check_types and severities.

SQL query identification keywords:
  - agg query:   "db_connections" OR "replica_lag_ms" OR "cursors" OR "buffer_cache_hit"
                 (single aggregation query keyed by which metric_type values appear in the IN clause)
  - INSERT:      starts with INSERT
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load():
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(
        "docdb_findings", _ROOT / "collectors/docdb_findings.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


df = _load()

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_MIN_CACHE_SAMPLES = 20  # minimum for low-cache-hit rule
_CLUSTER_ID = "docdb-test-cluster"
_FIXED_TS = "2026-06-12T00:00:00Z"


# ---------------------------------------------------------------------------
# Mock builder — branch on SQL keyword to inject aggregated metric rows
# ---------------------------------------------------------------------------


def _mock_execute(
    # connection saturation
    peak_db_connections=50.0,
    latest_db_connections_limit=100.0,
    # replica lag
    peak_replica_lag_ms=0.0,
    # cursor timeouts
    sum_cursors_timed_out=0.0,
    # buffer cache hit
    avg_buffer_cache_hit=99.0,
    cache_hit_samples=_MIN_CACHE_SAMPLES,
    # control: whether limit row is present
    limit_present=True,
):
    """Return a fake _execute side-effect that branches on SQL keywords."""

    def fake(rds, arn, secret, db, sql, params=None):
        sql_lower = sql.lower()

        if sql.strip().upper().startswith("INSERT"):
            return []

        # Single aggregation query — identified by metric_type names in the IN list
        # The collector issues one SELECT over metric_snapshots with multiple metrics.
        # We detect it by the presence of these metric names in the SQL.
        if "db_connections" in sql_lower and "replica_lag_ms" in sql_lower:
            row = {
                "peak_db_connections": peak_db_connections,
                "latest_db_connections_limit": latest_db_connections_limit if limit_present else None,
                "peak_replica_lag_ms": peak_replica_lag_ms,
                "sum_cursors_timed_out": sum_cursors_timed_out,
                "avg_buffer_cache_hit": avg_buffer_cache_hit,
                "cache_hit_samples": cache_hit_samples,
            }
            return [row]

        return []

    return fake


def _run(
    peak_db_connections=50.0,
    latest_db_connections_limit=100.0,
    peak_replica_lag_ms=0.0,
    sum_cursors_timed_out=0.0,
    avg_buffer_cache_hit=99.0,
    cache_hit_samples=_MIN_CACHE_SAMPLES,
    limit_present=True,
):
    """Run the collector with patched _execute; return (emitted list, result dict).

    Each emitted item: {"check_type", "severity", "value_str", "recommendation"}.
    """
    emitted = []
    side = _mock_execute(
        peak_db_connections=peak_db_connections,
        latest_db_connections_limit=latest_db_connections_limit,
        peak_replica_lag_ms=peak_replica_lag_ms,
        sum_cursors_timed_out=sum_cursors_timed_out,
        avg_buffer_cache_hit=avg_buffer_cache_hit,
        cache_hit_samples=cache_hit_samples,
        limit_present=limit_present,
    )

    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(
                    {
                        "check_type": params["check_type"],
                        "severity": params["severity"],
                        "value_str": params.get("value_str", ""),
                        "recommendation": params.get("recommendation", ""),
                    }
                )
            return side(rds, arn, secret, db, sql, params)

        mock_ex.side_effect = capture
        result = df.collect_docdb_findings(
            MagicMock(), "arn:cache", "secret:cache", "dbops", _CLUSTER_ID,
            snapshot_ts=_FIXED_TS,
        )

    return emitted, result


# ---------------------------------------------------------------------------
# Test 1 — connection saturation ≥ 80% → warning
# ---------------------------------------------------------------------------

def test_connection_saturation_warning():
    """peak_db_connections / db_connections_limit = 80% → docdb_connection_saturation warning."""
    emitted, result = _run(
        peak_db_connections=80.0,
        latest_db_connections_limit=100.0,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "docdb_connection_saturation" in check_types, (
        f"Expected docdb_connection_saturation, got: {check_types}"
    )
    finding = next(e for e in emitted if e["check_type"] == "docdb_connection_saturation")
    assert finding["severity"] == "warning"
    # value_str must show peak, limit and percentage
    assert "80" in finding["value_str"]
    assert "100" in finding["value_str"]
    assert result["findings_emitted"] >= 1


# ---------------------------------------------------------------------------
# Test 2 — connection saturation ≥ 95% → critical
# ---------------------------------------------------------------------------

def test_connection_saturation_critical():
    """peak / limit = 95% → docdb_connection_saturation critical."""
    emitted, _ = _run(
        peak_db_connections=95.0,
        latest_db_connections_limit=100.0,
    )
    finding = next(
        (e for e in emitted if e["check_type"] == "docdb_connection_saturation"), None
    )
    assert finding is not None, "Expected docdb_connection_saturation finding"
    assert finding["severity"] == "critical"


# ---------------------------------------------------------------------------
# Test 3 — connection saturation < 80% → silent
# ---------------------------------------------------------------------------

def test_connection_saturation_below_threshold_silent():
    """peak / limit = 50% → no docdb_connection_saturation finding."""
    emitted, _ = _run(
        peak_db_connections=50.0,
        latest_db_connections_limit=100.0,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "docdb_connection_saturation" not in check_types


# ---------------------------------------------------------------------------
# Test 4 — limit missing (NULL) → connection saturation rule silently skipped
# ---------------------------------------------------------------------------

def test_connection_saturation_limit_missing_silent():
    """When db_connections_limit is NULL/missing, the saturation rule must be silent."""
    emitted, _ = _run(
        peak_db_connections=99.0,
        latest_db_connections_limit=0.0,
        limit_present=False,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "docdb_connection_saturation" not in check_types, (
        f"Saturation rule must be silent when limit is missing, got: {check_types}"
    )


# ---------------------------------------------------------------------------
# Test 5 — limit = 0 → connection saturation rule silently skipped (division guard)
# ---------------------------------------------------------------------------

def test_connection_saturation_limit_zero_silent():
    """When db_connections_limit = 0, rule must be silently skipped (avoid div-by-zero)."""
    emitted, _ = _run(
        peak_db_connections=99.0,
        latest_db_connections_limit=0.0,
        limit_present=True,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "docdb_connection_saturation" not in check_types


# ---------------------------------------------------------------------------
# Test 6 — replica lag ≥ 1000 ms → warning
# ---------------------------------------------------------------------------

def test_replica_lag_warning():
    """peak_replica_lag_ms = 1000 → docdb_replica_lag warning."""
    emitted, _ = _run(peak_replica_lag_ms=1000.0)
    finding = next(
        (e for e in emitted if e["check_type"] == "docdb_replica_lag"), None
    )
    assert finding is not None, "Expected docdb_replica_lag finding"
    assert finding["severity"] == "warning"


# ---------------------------------------------------------------------------
# Test 7 — replica lag ≥ 10000 ms → critical
# ---------------------------------------------------------------------------

def test_replica_lag_critical():
    """peak_replica_lag_ms = 15000 → docdb_replica_lag critical."""
    emitted, _ = _run(peak_replica_lag_ms=15000.0)
    finding = next(
        (e for e in emitted if e["check_type"] == "docdb_replica_lag"), None
    )
    assert finding is not None, "Expected docdb_replica_lag finding"
    assert finding["severity"] == "critical"


# ---------------------------------------------------------------------------
# Test 8 — replica lag < 1000 ms → silent (single-instance cluster ~0)
# ---------------------------------------------------------------------------

def test_replica_lag_below_threshold_silent():
    """peak_replica_lag_ms = 0 (single-instance) → no docdb_replica_lag finding."""
    emitted, _ = _run(peak_replica_lag_ms=0.0)
    check_types = [e["check_type"] for e in emitted]
    assert "docdb_replica_lag" not in check_types


# ---------------------------------------------------------------------------
# Test 9 — cursor timeouts > 0 → warning
# ---------------------------------------------------------------------------

def test_cursor_timeout_warning():
    """sum_cursors_timed_out > 0 → docdb_cursor_timeout warning."""
    emitted, _ = _run(sum_cursors_timed_out=3.0)
    finding = next(
        (e for e in emitted if e["check_type"] == "docdb_cursor_timeout"), None
    )
    assert finding is not None, "Expected docdb_cursor_timeout finding"
    assert finding["severity"] == "warning"


# ---------------------------------------------------------------------------
# Test 10 — cursor timeouts = 0 → silent
# ---------------------------------------------------------------------------

def test_cursor_timeout_zero_silent():
    """sum_cursors_timed_out = 0 → no docdb_cursor_timeout finding."""
    emitted, _ = _run(sum_cursors_timed_out=0.0)
    check_types = [e["check_type"] for e in emitted]
    assert "docdb_cursor_timeout" not in check_types


# ---------------------------------------------------------------------------
# Test 11 — low buffer cache hit (avg < 95%, enough samples) → warning
# ---------------------------------------------------------------------------

def test_low_cache_hit_warning():
    """avg_buffer_cache_hit = 80% with ≥20 samples → docdb_low_cache_hit warning."""
    emitted, _ = _run(
        avg_buffer_cache_hit=80.0,
        cache_hit_samples=_MIN_CACHE_SAMPLES,
    )
    finding = next(
        (e for e in emitted if e["check_type"] == "docdb_low_cache_hit"), None
    )
    assert finding is not None, "Expected docdb_low_cache_hit finding"
    assert finding["severity"] == "warning"


# ---------------------------------------------------------------------------
# Test 12 — low cache hit but too few samples → silent (brand-new idle cluster)
# ---------------------------------------------------------------------------

def test_low_cache_hit_too_few_samples_silent():
    """avg_buffer_cache_hit = 80% but only 5 samples → no docdb_low_cache_hit (avoid false positive)."""
    emitted, _ = _run(
        avg_buffer_cache_hit=80.0,
        cache_hit_samples=5,  # < 20 → silent
    )
    check_types = [e["check_type"] for e in emitted]
    assert "docdb_low_cache_hit" not in check_types, (
        f"Must not flag low cache hit with too few samples, got: {check_types}"
    )


# ---------------------------------------------------------------------------
# Test 13 — cache hit ≥ 95% → silent
# ---------------------------------------------------------------------------

def test_cache_hit_adequate_silent():
    """avg_buffer_cache_hit = 97% → no docdb_low_cache_hit finding."""
    emitted, _ = _run(
        avg_buffer_cache_hit=97.0,
        cache_hit_samples=_MIN_CACHE_SAMPLES,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "docdb_low_cache_hit" not in check_types


# ---------------------------------------------------------------------------
# Test 14 — all metrics healthy → no findings
# ---------------------------------------------------------------------------

def test_all_healthy_no_findings():
    """Healthy cluster: low connections, no lag, no cursor timeouts, high cache → no findings."""
    emitted, result = _run(
        peak_db_connections=20.0,
        latest_db_connections_limit=100.0,
        peak_replica_lag_ms=50.0,
        sum_cursors_timed_out=0.0,
        avg_buffer_cache_hit=99.0,
        cache_hit_samples=_MIN_CACHE_SAMPLES,
    )
    assert emitted == [], f"Expected no findings for healthy cluster, got: {emitted}"
    assert result["findings_emitted"] == 0


# ---------------------------------------------------------------------------
# Test 15 — multiple rules fire simultaneously
# ---------------------------------------------------------------------------

def test_multiple_rules_fire_simultaneously():
    """High connections + high lag + cursor timeouts → multiple findings emitted."""
    emitted, result = _run(
        peak_db_connections=96.0,
        latest_db_connections_limit=100.0,
        peak_replica_lag_ms=12000.0,
        sum_cursors_timed_out=5.0,
        avg_buffer_cache_hit=99.0,
        cache_hit_samples=_MIN_CACHE_SAMPLES,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "docdb_connection_saturation" in check_types
    assert "docdb_replica_lag" in check_types
    assert "docdb_cursor_timeout" in check_types
    assert result["findings_emitted"] >= 3


# ---------------------------------------------------------------------------
# Test 16 — snapshot_ts is shared across all INSERTs (shared run_ts check)
# ---------------------------------------------------------------------------

def test_snapshot_ts_is_shared():
    """snapshot_ts passed in must appear verbatim as :ts param in every INSERT."""
    fixed_ts = "2026-06-12T06:30:00+00:00"
    ts_seen = []

    side = _mock_execute(
        peak_db_connections=96.0,
        latest_db_connections_limit=100.0,
        peak_replica_lag_ms=5000.0,
        sum_cursors_timed_out=1.0,
    )

    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                ts_seen.append(params.get("ts"))
            return side(rds, arn, secret, db, sql, params)

        mock_ex.side_effect = capture
        df.collect_docdb_findings(
            MagicMock(), "arn", "secret", "db", _CLUSTER_ID,
            snapshot_ts=fixed_ts,
        )

    assert ts_seen, "Expected at least one INSERT"
    assert all(ts == fixed_ts for ts in ts_seen), f"ts mismatch: {ts_seen}"


# ---------------------------------------------------------------------------
# Test 17 — empty result set from aggregation query → silent (missing inputs)
# ---------------------------------------------------------------------------

def test_empty_aggregation_result_silent():
    """When the aggregation query returns no rows, all rules must be silently skipped."""
    emitted = []

    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
            return []  # always return empty

        mock_ex.side_effect = capture
        result = df.collect_docdb_findings(
            MagicMock(), "arn", "secret", "db", _CLUSTER_ID,
            snapshot_ts=_FIXED_TS,
        )

    assert emitted == [], f"Expected no findings when no data, got: {emitted}"
    assert result["findings_emitted"] == 0


# ---------------------------------------------------------------------------
# Test 18 — result dict contains cluster_id and findings_emitted
# ---------------------------------------------------------------------------

def test_result_dict_structure():
    """collect_docdb_findings must return a dict with cluster_id and findings_emitted keys."""
    _, result = _run()
    assert result["cluster_id"] == _CLUSTER_ID
    assert "findings_emitted" in result
    assert isinstance(result["findings_emitted"], int)
