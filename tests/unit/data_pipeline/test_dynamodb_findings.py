"""DynamoDB findings collector — TDD test suite.

Strategy: patch _execute at the module level so every SQL call is intercepted.
Branch on SQL keywords to inject fake cache rows (cluster_meta billing_mode;
per-side throttle aggregates; lateral-join util queries), capture INSERTs,
assert emitted check_types and severities.

SQL query identification keywords (after Fix 1 & 2):
  - cluster_meta query:          "cluster_meta" + "resource_details"
  - throttle aggregate (Fix 1):  "read_throttle" (new per-side columns)
  - RCU util lateral (Fix 2):    "peak_util_r"  (or "consumed_rcu" + "LATERAL")
  - WCU util lateral (Fix 2):    "peak_util_w"  (or "consumed_wcu" + "LATERAL")
  - raw consumed (on-demand):    "max_consumed_rcu" without LATERAL
  - GSI throttle:                "gsi" + "throttle"
  - INSERT:                      starts with INSERT
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load():
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(
        "dynamodb_findings", _ROOT / "collectors/dynamodb_findings.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


df = _load()

# ---------------------------------------------------------------------------
# Shared mock builder (updated for Fix 1 + Fix 2 SQL shapes)
# ---------------------------------------------------------------------------

_CONSUMED_SAMPLES = 30  # enough samples for overprovisioned rule


def _mock_execute(
    billing_mode="PROVISIONED",
    # Fix 1: per-side throttle
    read_throttle=0.0,
    write_throttle=0.0,
    throttle_minutes=0,
    # Fix 2: per-minute lateral-join util results
    peak_util_r=0.0,
    peak_util_w=0.0,
    high_minutes_r=0,
    high_minutes_w=0,
    n_r=_CONSUMED_SAMPLES,   # >0 means provisioned data exists for read side
    n_w=_CONSUMED_SAMPLES,   # >0 means provisioned data exists for write side
    # raw consumed (for on-demand rule)
    max_consumed_rcu=0.0,
    max_consumed_wcu=0.0,
):
    """Return a fake _execute side-effect that branches on SQL keywords."""
    throttle_total = read_throttle + write_throttle

    def fake(rds, arn, secret, db, sql, params=None):
        import json as _json

        # cluster_meta resource_details query
        if "cluster_meta" in sql and "resource_details" in sql:
            return [{"resource_details": _json.dumps({"billing_mode": billing_mode})}]

        # per-side throttle aggregate (Fix 1) — keyed by "read_throttle" column name
        if "read_throttle" in sql and "write_throttle" in sql and "cluster_meta" not in sql:
            return [
                {
                    "read_throttle": float(read_throttle),
                    "write_throttle": float(write_throttle),
                    "throttle_total": float(throttle_total),
                    "throttle_minutes": int(throttle_minutes),
                }
            ]

        # RCU lateral-join util query (Fix 2) — keyed by "peak_util_r"
        if "peak_util_r" in sql:
            return [
                {
                    "peak_util_r": float(peak_util_r) if n_r > 0 else None,
                    "high_minutes_r": int(high_minutes_r),
                    "n_r": int(n_r),
                }
            ]

        # WCU lateral-join util query (Fix 2) — keyed by "peak_util_w"
        if "peak_util_w" in sql:
            return [
                {
                    "peak_util_w": float(peak_util_w) if n_w > 0 else None,
                    "high_minutes_w": int(high_minutes_w),
                    "n_w": int(n_w),
                }
            ]

        # raw consumed aggregate (for on-demand rule) — keyed by "max_consumed_rcu"
        if "max_consumed_rcu" in sql:
            return [
                {
                    "max_consumed_rcu": float(max_consumed_rcu),
                    "max_consumed_wcu": float(max_consumed_wcu),
                }
            ]

        if sql.strip().upper().startswith("INSERT"):
            return []
        return []

    return fake


def _run(
    billing_mode="PROVISIONED",
    read_throttle=0.0,
    write_throttle=0.0,
    throttle_minutes=0,
    peak_util_r=0.0,
    peak_util_w=0.0,
    high_minutes_r=0,
    high_minutes_w=0,
    n_r=_CONSUMED_SAMPLES,
    n_w=_CONSUMED_SAMPLES,
    max_consumed_rcu=0.0,
    max_consumed_wcu=0.0,
):
    """Run the collector with patched _execute; return (emitted list, result dict).

    Each emitted item: {"check_type", "severity", "value_str", "recommendation"}.
    """
    emitted = []
    side = _mock_execute(
        billing_mode=billing_mode,
        read_throttle=read_throttle,
        write_throttle=write_throttle,
        throttle_minutes=throttle_minutes,
        peak_util_r=peak_util_r,
        peak_util_w=peak_util_w,
        high_minutes_r=high_minutes_r,
        high_minutes_w=high_minutes_w,
        n_r=n_r,
        n_w=n_w,
        max_consumed_rcu=max_consumed_rcu,
        max_consumed_wcu=max_consumed_wcu,
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
        result = df.collect_dynamodb_findings(
            MagicMock(), "arn", "secret", "db", "tbl-1",
            snapshot_ts="2026-06-12T00:00:00Z",
        )

    return emitted, result


# ---------------------------------------------------------------------------
# Test 1 — heavy throttle (per-side) + low utilization → ddb_throttling (critical)
#          + ddb_hot_partition (Fix 1: write side throttle, low write util)
# ---------------------------------------------------------------------------

def test_heavy_throttle_low_util_critical_and_hot_partition():
    """≥100 throttle events + write util < 50% → critical throttling + hot_partition."""
    # write_throttle=150, peak_util_w=0.017 (< 50%) → hot_partition fires on write side
    emitted, result = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=150.0,
        throttle_minutes=12,   # ≥10 → critical
        peak_util_r=0.10,
        peak_util_w=0.017,     # 1.7% << 50% → hot partition (write side)
        high_minutes_r=0,
        high_minutes_w=0,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_throttling" in check_types
    throttle_finding = next(e for e in emitted if e["check_type"] == "ddb_throttling")
    assert throttle_finding["severity"] == "critical"

    assert "ddb_hot_partition" in check_types
    assert result["findings_emitted"] >= 2


# ---------------------------------------------------------------------------
# Test 2 — throttle ≥ 1 but < 100, < 10 minutes → warning throttle
# ---------------------------------------------------------------------------

def test_moderate_throttle_warning():
    """1 ≤ throttle < 100 AND throttle_minutes < 10 → warning (not critical)."""
    emitted, _ = _run(
        billing_mode="PROVISIONED",
        read_throttle=25.0,
        write_throttle=25.0,
        throttle_minutes=5,
        peak_util_r=0.05,
        peak_util_w=0.05,
        high_minutes_r=0,
        high_minutes_w=0,
    )
    throttle_findings = [e for e in emitted if e["check_type"] == "ddb_throttling"]
    assert len(throttle_findings) == 1
    assert throttle_findings[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# Test 3 — high utilization (≥80% sustained), no throttle
#          → ddb_capacity_underprovisioned (Fix 4: ≥3 high_minutes)
# ---------------------------------------------------------------------------

def test_high_utilization_sustained_underprovisioned():
    """Peak RCU util ≥ 80% sustained ≥3 minutes, no throttle → ddb_capacity_underprovisioned."""
    emitted, result = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=0.0,
        throttle_minutes=0,
        peak_util_r=1.0,        # 100%
        peak_util_w=0.10,
        high_minutes_r=5,       # ≥3 → fires
        high_minutes_w=0,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_capacity_underprovisioned" in check_types
    under = next(e for e in emitted if e["check_type"] == "ddb_capacity_underprovisioned")
    assert under["severity"] == "warning"
    assert "ddb_throttling" not in check_types


# ---------------------------------------------------------------------------
# Test 4 — idle provisioned (low util, enough samples) → ddb_capacity_overprovisioned
# ---------------------------------------------------------------------------

def test_idle_provisioned_overprovisioned():
    """Both RCU and WCU peak util ≤ 20% with ≥20 consumed datapoints → ddb_capacity_overprovisioned."""
    emitted, result = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=0.0,
        throttle_minutes=0,
        peak_util_r=0.01,   # 1%
        peak_util_w=0.01,   # 1%
        high_minutes_r=0,
        high_minutes_w=0,
        n_r=25,
        n_w=25,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_capacity_overprovisioned" in check_types
    over = next(e for e in emitted if e["check_type"] == "ddb_capacity_overprovisioned")
    assert over["severity"] == "info"


# ---------------------------------------------------------------------------
# Test 5 — idle provisioned but too few samples → no overprovisioned finding
# ---------------------------------------------------------------------------

def test_idle_provisioned_too_few_samples_no_finding():
    """Same low utilization but < 20 consumed datapoints → should NOT flag overprovisioned."""
    emitted, _ = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=0.0,
        peak_util_r=0.01,
        peak_util_w=0.01,
        high_minutes_r=0,
        high_minutes_w=0,
        n_r=5,    # too few
        n_w=5,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_capacity_overprovisioned" not in check_types


# ---------------------------------------------------------------------------
# Test 6 — PAY_PER_REQUEST: no provisioned rules; high consumed → ddb_ondemand_high_throughput
# ---------------------------------------------------------------------------

def test_pay_per_request_high_consumed_ondemand_info():
    """PAY_PER_REQUEST + max consumed ≥ 6000 → ddb_ondemand_high_throughput (info).
    Provisioned / hot-partition rules must NOT fire."""
    emitted, result = _run(
        billing_mode="PAY_PER_REQUEST",
        read_throttle=0.0,
        write_throttle=0.0,
        peak_util_r=0.0,
        peak_util_w=0.0,
        high_minutes_r=0,
        high_minutes_w=0,
        n_r=0,    # no provisioned data
        n_w=0,
        max_consumed_rcu=7000.0,
        max_consumed_wcu=7000.0,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_ondemand_high_throughput" in check_types
    od = next(e for e in emitted if e["check_type"] == "ddb_ondemand_high_throughput")
    assert od["severity"] == "info"

    # Provisioned-only rules must not fire
    assert "ddb_capacity_underprovisioned" not in check_types
    assert "ddb_capacity_overprovisioned" not in check_types
    assert "ddb_hot_partition" not in check_types
    assert result["billing_mode"] == "PAY_PER_REQUEST"


# ---------------------------------------------------------------------------
# Test 7 — PAY_PER_REQUEST, low throughput → no finding
# ---------------------------------------------------------------------------

def test_pay_per_request_low_throughput_no_finding():
    """PAY_PER_REQUEST with consumed < 6000 → no findings."""
    emitted, _ = _run(
        billing_mode="PAY_PER_REQUEST",
        read_throttle=0.0,
        write_throttle=0.0,
        peak_util_r=0.0,
        peak_util_w=0.0,
        n_r=0,
        n_w=0,
        max_consumed_rcu=100.0,
        max_consumed_wcu=100.0,
    )
    assert emitted == []


# ---------------------------------------------------------------------------
# Test 8 — healthy provisioned (no throttle, util 30–70%) → no findings
# ---------------------------------------------------------------------------

def test_healthy_provisioned_no_findings():
    """Moderate utilization (30–70%), no throttle, enough samples → no findings."""
    emitted, _ = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=0.0,
        peak_util_r=0.50,
        peak_util_w=0.50,
        high_minutes_r=0,
        high_minutes_w=0,
    )
    assert emitted == []


# ---------------------------------------------------------------------------
# Test 9 — missing provisioned data → provisioned rules silently skip
# ---------------------------------------------------------------------------

def test_missing_provisioned_data_skips_provisioned_rules():
    """When prov data is missing (n_r=0, n_w=0), provisioned rules must be silently skipped."""
    emitted, _ = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=0.0,
        peak_util_r=0.0,
        peak_util_w=0.0,
        n_r=0,   # no provisioned data → util unknown
        n_w=0,
        max_consumed_rcu=5000.0,
        max_consumed_wcu=5000.0,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_capacity_underprovisioned" not in check_types
    assert "ddb_hot_partition" not in check_types


# ---------------------------------------------------------------------------
# Test 10 — resource_details comes back as a raw string (JSONB → str) → parsed correctly
# ---------------------------------------------------------------------------

def test_billing_mode_string_jsonb_parsed():
    """resource_details may be a plain string from RDS Data API (JSONB stringValue);
    the collector must json.loads it and read billing_mode correctly."""
    import json

    def fake_string_jsonb(rds, arn, secret, db, sql, params=None):
        if "cluster_meta" in sql and "resource_details" in sql:
            return [{"resource_details": json.dumps({"billing_mode": "PAY_PER_REQUEST"})}]
        if "read_throttle" in sql and "write_throttle" in sql:
            return [{"read_throttle": 0.0, "write_throttle": 0.0,
                     "throttle_total": 0.0, "throttle_minutes": 0}]
        if "peak_util_r" in sql:
            return [{"peak_util_r": None, "high_minutes_r": 0, "n_r": 0}]
        if "peak_util_w" in sql:
            return [{"peak_util_w": None, "high_minutes_w": 0, "n_w": 0}]
        if "max_consumed_rcu" in sql:
            return [{"max_consumed_rcu": 100.0, "max_consumed_wcu": 100.0}]
        if sql.strip().upper().startswith("INSERT"):
            return []
        return []

    emitted = []
    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
            return fake_string_jsonb(rds, arn, secret, db, sql, params)

        mock_ex.side_effect = capture
        result = df.collect_dynamodb_findings(
            MagicMock(), "arn", "secret", "db", "tbl-str",
            snapshot_ts="2026-06-12T00:00:00Z",
        )

    assert result["billing_mode"] == "PAY_PER_REQUEST"
    assert "ddb_capacity_underprovisioned" not in emitted
    assert "ddb_capacity_overprovisioned" not in emitted


# ---------------------------------------------------------------------------
# Test 11 — run_ts is stored verbatim in snapshot_time param (shared ts check)
# ---------------------------------------------------------------------------

def test_snapshot_ts_is_shared():
    """snapshot_ts passed in must appear verbatim as :ts param in every INSERT."""
    fixed_ts = "2026-06-12T06:30:00Z"

    ts_seen = []
    side = _mock_execute(
        billing_mode="PROVISIONED",
        read_throttle=100.0,
        write_throttle=100.0,
        throttle_minutes=15,
        peak_util_r=0.10,
        peak_util_w=0.10,
        high_minutes_r=0,
        high_minutes_w=0,
    )

    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                ts_seen.append(params.get("ts"))
            return side(rds, arn, secret, db, sql, params)

        mock_ex.side_effect = capture
        df.collect_dynamodb_findings(
            MagicMock(), "arn", "secret", "db", "tbl-ts",
            snapshot_ts=fixed_ts,
        )

    assert ts_seen, "Expected at least one INSERT"
    assert all(ts == fixed_ts for ts in ts_seen), f"ts mismatch: {ts_seen}"


# ---------------------------------------------------------------------------
# Test 12 — per-GSI throttle > 0 → ddb_gsi_throttling (warning)
# ---------------------------------------------------------------------------

def test_gsi_throttling_emits_finding_when_throttle_nonzero():
    """When per-GSI throttle query returns a GSI with SUM > 0,
    a ddb_gsi_throttling warning finding must be emitted."""
    gsi_name = "gsi-status"

    def fake_with_gsi(rds, arn, secret, db, sql, params=None):
        import json as _json
        if "cluster_meta" in sql and "resource_details" in sql:
            return [{"resource_details": _json.dumps({"billing_mode": "PROVISIONED"})}]
        if "read_throttle" in sql and "write_throttle" in sql and "gsi" not in sql:
            return [{"read_throttle": 0.0, "write_throttle": 0.0,
                     "throttle_total": 0.0, "throttle_minutes": 0}]
        if "peak_util_r" in sql:
            return [{"peak_util_r": 0.01, "high_minutes_r": 0, "n_r": _CONSUMED_SAMPLES}]
        if "peak_util_w" in sql:
            return [{"peak_util_w": 0.01, "high_minutes_w": 0, "n_w": _CONSUMED_SAMPLES}]
        if "max_consumed_rcu" in sql:
            return [{"max_consumed_rcu": 60.0, "max_consumed_wcu": 60.0}]
        # per-GSI throttle query
        if "gsi" in sql and "throttle" in sql:
            return [{"gsi_name": gsi_name, "gsi_throttle_total": 42.0}]
        if sql.strip().upper().startswith("INSERT"):
            return []
        return []

    emitted = []
    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append({
                    "check_type": params["check_type"],
                    "severity": params["severity"],
                    "subject": params.get("subject", ""),
                    "value_str": params.get("value_str", ""),
                })
            return fake_with_gsi(rds, arn, secret, db, sql, params)

        mock_ex.side_effect = capture
        result = df.collect_dynamodb_findings(
            MagicMock(), "arn", "secret", "db", "tbl-gsi",
            snapshot_ts="2026-06-12T00:00:00Z",
        )

    gsi_findings = [e for e in emitted if e["check_type"] == "ddb_gsi_throttling"]
    assert len(gsi_findings) >= 1, f"Expected ddb_gsi_throttling finding, got: {emitted}"
    finding = gsi_findings[0]
    assert finding["severity"] == "warning"
    assert gsi_name in finding["subject"] or gsi_name in finding["value_str"], (
        f"GSI name {gsi_name!r} must appear in subject or value_str: {finding}"
    )


# ---------------------------------------------------------------------------
# Test 13 — per-GSI throttle = 0 for all GSIs → no ddb_gsi_throttling finding
# ---------------------------------------------------------------------------

def test_gsi_throttling_silent_when_all_zero():
    """When per-GSI query returns rows with throttle_total = 0 (or no rows),
    ddb_gsi_throttling must NOT be emitted."""

    def fake_no_gsi_throttle(rds, arn, secret, db, sql, params=None):
        import json as _json
        if "cluster_meta" in sql and "resource_details" in sql:
            return [{"resource_details": _json.dumps({"billing_mode": "PROVISIONED"})}]
        if "read_throttle" in sql and "write_throttle" in sql and "gsi" not in sql:
            return [{"read_throttle": 0.0, "write_throttle": 0.0,
                     "throttle_total": 0.0, "throttle_minutes": 0}]
        if "peak_util_r" in sql:
            return [{"peak_util_r": 0.01, "high_minutes_r": 0, "n_r": _CONSUMED_SAMPLES}]
        if "peak_util_w" in sql:
            return [{"peak_util_w": 0.01, "high_minutes_w": 0, "n_w": _CONSUMED_SAMPLES}]
        if "max_consumed_rcu" in sql:
            return [{"max_consumed_rcu": 60.0, "max_consumed_wcu": 60.0}]
        if "gsi" in sql and "throttle" in sql:
            return [{"gsi_name": "gsi-safe", "gsi_throttle_total": 0.0}]
        if sql.strip().upper().startswith("INSERT"):
            return []
        return []

    emitted = []
    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
            return fake_no_gsi_throttle(rds, arn, secret, db, sql, params)

        mock_ex.side_effect = capture
        df.collect_dynamodb_findings(
            MagicMock(), "arn", "secret", "db", "tbl-clean",
            snapshot_ts="2026-06-12T00:00:00Z",
        )

    assert "ddb_gsi_throttling" not in emitted, (
        f"ddb_gsi_throttling should not fire when all GSI throttle = 0, got: {emitted}"
    )


# ---------------------------------------------------------------------------
# Test 14 — no per-GSI rows at all → silent skip (no ddb_gsi_throttling)
# ---------------------------------------------------------------------------

def test_gsi_throttling_silent_when_no_gsi_rows():
    """When the per-GSI query returns an empty result set (no GSIs exist),
    ddb_gsi_throttling must be silently skipped."""

    def fake_empty_gsi(rds, arn, secret, db, sql, params=None):
        import json as _json
        if "cluster_meta" in sql and "resource_details" in sql:
            return [{"resource_details": _json.dumps({"billing_mode": "PAY_PER_REQUEST"})}]
        if "read_throttle" in sql and "write_throttle" in sql:
            return [{"read_throttle": 0.0, "write_throttle": 0.0,
                     "throttle_total": 0.0, "throttle_minutes": 0}]
        if "peak_util_r" in sql:
            return [{"peak_util_r": None, "high_minutes_r": 0, "n_r": 0}]
        if "peak_util_w" in sql:
            return [{"peak_util_w": None, "high_minutes_w": 0, "n_w": 0}]
        if "max_consumed_rcu" in sql:
            return [{"max_consumed_rcu": 100.0, "max_consumed_wcu": 100.0}]
        if "gsi" in sql and "throttle" in sql:
            return []
        if sql.strip().upper().startswith("INSERT"):
            return []
        return []

    emitted = []
    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
            return fake_empty_gsi(rds, arn, secret, db, sql, params)

        mock_ex.side_effect = capture
        df.collect_dynamodb_findings(
            MagicMock(), "arn", "secret", "db", "tbl-nogsi",
            snapshot_ts="2026-06-12T00:00:00Z",
        )

    assert "ddb_gsi_throttling" not in emitted


# ===========================================================================
# NEW TESTS for Fix 1–4
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 15 (Fix 4) — write throttles + HIGH util_w (≥80% sustained ≥3 min)
#   → ddb_capacity_underprovisioned fires, ddb_hot_partition does NOT
# ---------------------------------------------------------------------------

def test_write_throttle_high_util_underprovisioned_not_hot_partition():
    """write_throttle > 0 AND peak_util_w ≥ 80% sustained ≥3 min
    → ddb_capacity_underprovisioned fires; ddb_hot_partition does NOT (util too high)."""
    emitted, result = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=50.0,
        throttle_minutes=5,
        peak_util_r=0.10,
        peak_util_w=0.90,       # 90% ≥ 80%, ≥ 50% → hot_partition silent
        high_minutes_r=0,
        high_minutes_w=4,       # ≥3 → underprovisioned fires
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_capacity_underprovisioned" in check_types, (
        f"Expected ddb_capacity_underprovisioned, got: {check_types}"
    )
    assert "ddb_hot_partition" not in check_types, (
        f"ddb_hot_partition must NOT fire when write util >= 50%, got: {check_types}"
    )


# ---------------------------------------------------------------------------
# Test 16 (Fix 1) — write throttles + LOW util_w (<50%)
#   → ddb_hot_partition fires
# ---------------------------------------------------------------------------

def test_write_throttle_low_util_hot_partition():
    """write_throttle > 0 AND peak_util_w < 50% → ddb_hot_partition fires."""
    emitted, result = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=30.0,
        throttle_minutes=3,
        peak_util_r=0.10,
        peak_util_w=0.20,       # 20% < 50% → hot_partition fires
        high_minutes_r=0,
        high_minutes_w=0,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_hot_partition" in check_types, (
        f"Expected ddb_hot_partition for low write util, got: {check_types}"
    )
    # Verify the finding details reflect write side
    hot = next(e for e in emitted if e["check_type"] == "ddb_hot_partition")
    assert hot["severity"] == "warning"


# ---------------------------------------------------------------------------
# Test 17 (Fix 1) — read throttles + LOW util_r (<50%)
#   → ddb_hot_partition fires (read side)
# ---------------------------------------------------------------------------

def test_read_throttle_low_util_hot_partition_read_side():
    """read_throttle > 0 AND peak_util_r < 50% → ddb_hot_partition fires (read side)."""
    emitted, result = _run(
        billing_mode="PROVISIONED",
        read_throttle=20.0,
        write_throttle=0.0,
        throttle_minutes=2,
        peak_util_r=0.15,       # 15% < 50% → hot_partition fires
        peak_util_w=0.60,       # write side has no throttle → silent
        high_minutes_r=0,
        high_minutes_w=0,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_hot_partition" in check_types, (
        f"Expected ddb_hot_partition for low read util, got: {check_types}"
    )


# ---------------------------------------------------------------------------
# Test 18 (Fix 1) — write throttle but write util UNKNOWN (no provisioned)
#   → ddb_hot_partition silent
# ---------------------------------------------------------------------------

def test_write_throttle_unknown_write_util_hot_partition_silent():
    """write_throttle > 0 but n_w=0 (no provisioned data → util unknown)
    → ddb_hot_partition must NOT fire (silent when side-util unknown)."""
    emitted, result = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=50.0,
        throttle_minutes=3,
        peak_util_r=0.10,
        peak_util_w=0.0,
        n_r=_CONSUMED_SAMPLES,
        n_w=0,                  # no provisioned WCU data → peak_util_w is None
        high_minutes_r=0,
        high_minutes_w=0,
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_hot_partition" not in check_types, (
        f"ddb_hot_partition must be silent when write util is unknown, got: {check_types}"
    )


# ---------------------------------------------------------------------------
# Test 19 (Fix 3) — util >100% → recommendation contains burst explanation
# ---------------------------------------------------------------------------

def test_util_over_100_burst_explanation_in_recommendation():
    """When peak_util_w > 1.0 (e.g. 525%), value_str / recommendation
    must contain the burst explanation text."""
    emitted, result = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=0.0,
        throttle_minutes=0,
        peak_util_r=0.10,
        peak_util_w=5.25,       # 525% > 100%
        high_minutes_r=0,
        high_minutes_w=4,       # sustained → underprovisioned fires
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_capacity_underprovisioned" in check_types
    under = next(e for e in emitted if e["check_type"] == "ddb_capacity_underprovisioned")
    # Fix 3: burst explanation must appear in value_str or recommendation
    combined = under["value_str"] + under["recommendation"]
    assert "burst" in combined or ">100%" in combined, (
        f"Burst explanation missing from finding. value_str={under['value_str']!r}, "
        f"recommendation={under['recommendation']!r}"
    )


# ---------------------------------------------------------------------------
# Test 20 (Fix 4) — sustained requires ≥3 high minutes; 2 high minutes → silent
# ---------------------------------------------------------------------------

def test_underprovisioned_requires_3_high_minutes_two_is_not_enough():
    """high_minutes_r=2 AND high_minutes_w=2 (both < 3) → ddb_capacity_underprovisioned
    must NOT fire (not enough sustained high utilization)."""
    emitted, _ = _run(
        billing_mode="PROVISIONED",
        read_throttle=0.0,
        write_throttle=0.0,
        peak_util_r=0.85,
        peak_util_w=0.85,
        high_minutes_r=2,   # < 3 → not sustained
        high_minutes_w=2,   # < 3 → not sustained
    )
    check_types = [e["check_type"] for e in emitted]
    assert "ddb_capacity_underprovisioned" not in check_types, (
        f"ddb_capacity_underprovisioned must NOT fire with only 2 high minutes, got: {check_types}"
    )


# ---------------------------------------------------------------------------
# Test 21 — healthy / missing inputs → completely silent
# ---------------------------------------------------------------------------

def test_completely_missing_cluster_meta_silent():
    """When cluster_meta returns no rows (unknown billing_mode), all provisioned
    rules must be silently skipped."""

    def fake_no_meta(rds, arn, secret, db, sql, params=None):
        if "cluster_meta" in sql:
            return []   # no rows → billing_mode = None
        if "read_throttle" in sql and "write_throttle" in sql:
            return [{"read_throttle": 0.0, "write_throttle": 0.0,
                     "throttle_total": 0.0, "throttle_minutes": 0}]
        if "peak_util_r" in sql:
            return [{"peak_util_r": 0.0, "high_minutes_r": 0, "n_r": 0}]
        if "peak_util_w" in sql:
            return [{"peak_util_w": 0.0, "high_minutes_w": 0, "n_w": 0}]
        if "max_consumed_rcu" in sql:
            return [{"max_consumed_rcu": 0.0, "max_consumed_wcu": 0.0}]
        if "gsi" in sql:
            return []
        if sql.strip().upper().startswith("INSERT"):
            return []
        return []

    emitted = []
    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
            return fake_no_meta(rds, arn, secret, db, sql, params)

        mock_ex.side_effect = capture
        result = df.collect_dynamodb_findings(
            MagicMock(), "arn", "secret", "db", "tbl-nometa",
            snapshot_ts="2026-06-12T00:00:00Z",
        )

    assert emitted == [], f"Expected no findings for unknown billing_mode, got: {emitted}"
    assert result["billing_mode"] is None
