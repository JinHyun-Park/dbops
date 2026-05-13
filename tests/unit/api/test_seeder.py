"""Unit tests for api/clusters/seeder pure helpers.

Verifies the math behind synthetic data generation without touching AWS —
catches regressions in:
  - Timestamp formatting (RDS Data API rejects trailing tz suffix).
  - Metric value generator boundary behavior (must stay non-negative, must
    spike at the configured hour).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "api" / "clusters"))

import seeder  # type: ignore


def test_ts_param_strips_timezone_suffix():
    """RDS Data API's TIMESTAMP typeHint rejects '+00:00' in the string —
    the seeder strips tzinfo before formatting."""
    dt = datetime(2026, 5, 13, 14, 30, 0, tzinfo=timezone.utc)
    p = seeder._ts_param("ts", dt)
    val = p["value"]["stringValue"]
    assert "+" not in val and "Z" not in val, f"unexpected tz suffix: {val!r}"
    assert val == "2026-05-13 14:30:00"


def test_metric_value_never_negative():
    """`deadlocks` baseline is 0 with noise — value must clamp to 0."""
    profile = seeder.METRIC_PROFILES["deadlocks"]
    import random
    rng = random.Random(42)
    for hour in range(24):
        for minute in range(0, 60, 5):
            v = seeder._metric_value(profile, hour, minute, rng)
            assert v >= 0, f"got {v} at h={hour} m={minute}"


def test_metric_value_spikes_at_configured_hour():
    """CPU profile has a spike at hour 14 — value must hit at least the
    spike threshold."""
    profile = seeder.METRIC_PROFILES["cpu"]
    spike_hour, spike_val = profile[3], profile[4]
    import random
    rng = random.Random(0)
    v = seeder._metric_value(profile, spike_hour, 20, rng)
    assert v >= spike_val, f"expected spike >= {spike_val}, got {v}"


def test_sample_cluster_id_is_stable():
    """Frontend references `sample-cluster` as the demo identifier — must
    not change without intentional cascade."""
    assert seeder.SAMPLE_CLUSTER_ID == "sample-cluster"


def test_findings_use_collector_shape():
    """Frontend VacuumPanel + MaintenanceHealthPanel parse findings details
    via `{schema, table, age}` shape — the seeder must mirror it."""
    txid_findings = [f for f in seeder.FINDINGS if f[0] == "txid_age"]
    assert len(txid_findings) >= 1, "demo must seed at least one TXID age finding"
    for f in txid_findings:
        details = f[6]
        assert "schema" in details and "table" in details and "age" in details
