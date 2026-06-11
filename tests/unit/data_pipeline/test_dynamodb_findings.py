"""DynamoDB findings collector — TDD test suite.

Strategy: patch _execute at the module level so every SQL call is intercepted.
Branch on SQL keywords to inject fake cache rows (cluster_meta billing_mode;
throttle/consumed/provisioned aggregates), capture INSERTs, assert emitted
check_types and severities.
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
# Shared mock builder
# ---------------------------------------------------------------------------

_CONSUMED_SAMPLES = 30  # enough samples for overprovisioned rule


def _mock_execute(
    billing_mode="PROVISIONED",
    throttle_total=0,
    throttle_minutes=0,
    max_consumed_rcu=0.0,
    max_consumed_wcu=0.0,
    prov_rcu=0.0,
    prov_wcu=0.0,
    consumed_datapoints=_CONSUMED_SAMPLES,
):
    """Return a fake _execute side-effect that branches on SQL keywords."""

    def fake(rds, arn, secret, db, sql, params=None):
        # cluster_meta resource_details query
        if "cluster_meta" in sql and "resource_details" in sql:
            import json
            return [{"resource_details": json.dumps({"billing_mode": billing_mode})}]

        # throttle aggregate
        if "throttle_total" in sql or (
            "read_throttle_events" in sql and "throttle_minutes" in sql
        ):
            return [
                {
                    "throttle_total": float(throttle_total),
                    "throttle_minutes": int(throttle_minutes),
                }
            ]

        # consumed / provisioned aggregate
        if "max_consumed_rcu" in sql or "consumed_rcu" in sql:
            return [
                {
                    "max_consumed_rcu": float(max_consumed_rcu),
                    "max_consumed_wcu": float(max_consumed_wcu),
                    "prov_rcu": float(prov_rcu) if prov_rcu else None,
                    "prov_wcu": float(prov_wcu) if prov_wcu else None,
                    "consumed_datapoints": int(consumed_datapoints),
                }
            ]

        if sql.strip().upper().startswith("INSERT"):
            return []
        return []

    return fake


def _run(
    billing_mode="PROVISIONED",
    throttle_total=0,
    throttle_minutes=0,
    max_consumed_rcu=0.0,
    max_consumed_wcu=0.0,
    prov_rcu=0.0,
    prov_wcu=0.0,
    consumed_datapoints=_CONSUMED_SAMPLES,
):
    """Run the collector with patched _execute; return (emitted check_types list, result dict)."""
    emitted = []
    side = _mock_execute(
        billing_mode=billing_mode,
        throttle_total=throttle_total,
        throttle_minutes=throttle_minutes,
        max_consumed_rcu=max_consumed_rcu,
        max_consumed_wcu=max_consumed_wcu,
        prov_rcu=prov_rcu,
        prov_wcu=prov_wcu,
        consumed_datapoints=consumed_datapoints,
    )

    with patch.object(df, "_execute") as mock_ex:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(
                    {
                        "check_type": params["check_type"],
                        "severity": params["severity"],
                        "value_str": params.get("value_str", ""),
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
# Test 1 — heavy throttle + low utilization → ddb_throttling (critical) + ddb_hot_partition
# ---------------------------------------------------------------------------

def test_heavy_throttle_low_util_critical_and_hot_partition():
    """≥100 throttle events + utilization < 50% → critical throttling + hot partition."""
    emitted, result = _run(
        billing_mode="PROVISIONED",
        throttle_total=150,
        throttle_minutes=12,   # ≥10 → critical
        max_consumed_rcu=10.0,
        max_consumed_wcu=5.0,
        prov_rcu=10.0,  # peak util_r = 10/(60*10) ≈ 1.7% << 50%
        prov_wcu=10.0,
        consumed_datapoints=_CONSUMED_SAMPLES,
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
        throttle_total=50,
        throttle_minutes=5,
        max_consumed_rcu=1.0,
        max_consumed_wcu=1.0,
        prov_rcu=100.0,
        prov_wcu=100.0,
        consumed_datapoints=_CONSUMED_SAMPLES,
    )
    throttle_findings = [e for e in emitted if e["check_type"] == "ddb_throttling"]
    assert len(throttle_findings) == 1
    assert throttle_findings[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# Test 3 — high utilization (≥80%), no throttle → ddb_capacity_underprovisioned
# ---------------------------------------------------------------------------

def test_high_utilization_no_throttle_underprovisioned():
    """Peak RCU utilization ≥ 80% with no throttle → ddb_capacity_underprovisioned (warning)."""
    # peak util_r = 3000 / (60 * 50) = 1.0 = 100% ≥ 80%
    emitted, result = _run(
        billing_mode="PROVISIONED",
        throttle_total=0,
        throttle_minutes=0,
        max_consumed_rcu=3000.0,
        max_consumed_wcu=10.0,
        prov_rcu=50.0,
        prov_wcu=50.0,
        consumed_datapoints=_CONSUMED_SAMPLES,
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
    """Both RCU and WCU peak util ≤ 20% with ≥20 consumed datapoints → ddb_capacity_overprovisioned (info)."""
    # peak util_r = 60 / (60 * 100) = 1% ≤ 20%
    # peak util_w = 60 / (60 * 100) = 1% ≤ 20%
    emitted, result = _run(
        billing_mode="PROVISIONED",
        throttle_total=0,
        throttle_minutes=0,
        max_consumed_rcu=60.0,
        max_consumed_wcu=60.0,
        prov_rcu=100.0,
        prov_wcu=100.0,
        consumed_datapoints=25,
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
        throttle_total=0,
        throttle_minutes=0,
        max_consumed_rcu=60.0,
        max_consumed_wcu=60.0,
        prov_rcu=100.0,
        prov_wcu=100.0,
        consumed_datapoints=5,   # too few
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
        throttle_total=0,
        throttle_minutes=0,
        max_consumed_rcu=7000.0,
        max_consumed_wcu=7000.0,
        prov_rcu=0.0,
        prov_wcu=0.0,
        consumed_datapoints=_CONSUMED_SAMPLES,
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
        throttle_total=0,
        throttle_minutes=0,
        max_consumed_rcu=100.0,
        max_consumed_wcu=100.0,
        prov_rcu=0.0,
        prov_wcu=0.0,
        consumed_datapoints=_CONSUMED_SAMPLES,
    )
    assert emitted == []


# ---------------------------------------------------------------------------
# Test 8 — healthy provisioned (no throttle, util 30–70%) → no findings
# ---------------------------------------------------------------------------

def test_healthy_provisioned_no_findings():
    """Moderate utilization (30–70%), no throttle, enough samples → no findings."""
    # peak util_r = 1500 / (60 * 50) = 0.5 = 50%   (between 20% and 80%)
    emitted, _ = _run(
        billing_mode="PROVISIONED",
        throttle_total=0,
        throttle_minutes=0,
        max_consumed_rcu=1500.0,
        max_consumed_wcu=1500.0,
        prov_rcu=50.0,
        prov_wcu=50.0,
        consumed_datapoints=_CONSUMED_SAMPLES,
    )
    assert emitted == []


# ---------------------------------------------------------------------------
# Test 9 — missing provisioned data → provisioned rules silently skip
# ---------------------------------------------------------------------------

def test_missing_provisioned_data_skips_provisioned_rules():
    """When prov_rcu is None/0 (e.g. fresh PAY_PER_REQUEST→PROVISIONED flip),
    provisioned-mode rules requiring prov>0 must be silently skipped."""
    emitted, _ = _run(
        billing_mode="PROVISIONED",
        throttle_total=0,
        throttle_minutes=0,
        max_consumed_rcu=5000.0,
        max_consumed_wcu=5000.0,
        prov_rcu=0.0,   # guard: divide-by-zero must be avoided
        prov_wcu=0.0,
        consumed_datapoints=_CONSUMED_SAMPLES,
    )
    check_types = [e["check_type"] for e in emitted]
    # Without prov data we cannot compute utilization → skip underprovisioned + hot_partition
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
            # Return the JSON as a raw string (simulates RDS Data API stringValue)
            return [{"resource_details": json.dumps({"billing_mode": "PAY_PER_REQUEST"})}]
        if "throttle_total" in sql or (
            "read_throttle_events" in sql and "throttle_minutes" in sql
        ):
            return [{"throttle_total": 0.0, "throttle_minutes": 0}]
        if "max_consumed_rcu" in sql or "consumed_rcu" in sql:
            return [
                {
                    "max_consumed_rcu": 100.0,
                    "max_consumed_wcu": 100.0,
                    "prov_rcu": None,
                    "prov_wcu": None,
                    "consumed_datapoints": _CONSUMED_SAMPLES,
                }
            ]
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

    # Must parse to PAY_PER_REQUEST and skip provisioned rules
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
        throttle_total=200,
        throttle_minutes=15,
        max_consumed_rcu=10.0,
        max_consumed_wcu=10.0,
        prov_rcu=10.0,
        prov_wcu=10.0,
        consumed_datapoints=_CONSUMED_SAMPLES,
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
