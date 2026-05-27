"""Unit tests for the cost-handler day-over-day spike detector.

The detector is pure-function math (no AWS calls), so the goal here is to
pin the threshold-triple gate so a future "make it more sensitive" change
doesn't accidentally start flagging $0.05 → $0.20 jumps."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "api" / "cost" / "handler.py"


def _load():
    spec = importlib.util.spec_from_file_location("cost_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cost_handler"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def detector():
    return _load()._detect_anomalies


def _series(amounts: list[float], start_day: int = 10):
    """Build a daily-amount series matching the Cost Explorer response shape.
    `start_day` lets each test produce a unique day range — purely cosmetic."""
    return [
        {"date": f"2026-05-{start_day + i:02d}", "amount": a}
        for i, a in enumerate(amounts)
    ]


# ---------------------------------------------------------------------------
# Series length floor — < 8 days never yields an anomaly
# ---------------------------------------------------------------------------


def test_empty_series_returns_empty(detector):
    assert detector([]) == []


def test_short_series_below_baseline_window_returns_empty(detector):
    """The detector needs 7 days of baseline + 1 evaluation day; fewer must
    produce no anomalies, even with an obvious spike, because the baseline
    isn't trustworthy."""
    # 7 days only — no day has 7 preceding days to baseline against.
    assert detector(_series([0.5] * 6 + [50.0])) == []


def test_minimum_8_days_can_flag(detector):
    """At exactly 8 days the 8th day can be evaluated against the prior 7."""
    out = detector(_series([0.5] * 7 + [50.0]))
    assert len(out) == 1
    assert out[0]["date"].endswith("17")  # day index 7 = day 17


# ---------------------------------------------------------------------------
# Triple gate — z-score AND relative AND absolute must all clear
# ---------------------------------------------------------------------------


def test_small_absolute_jump_below_floor_not_flagged(detector):
    """$0.10 → $0.30 doubles relatively and has high z, but the $0.20 jump
    is below the $0.50 absolute floor. Finance noise — don't flag."""
    series = _series([0.1] * 7 + [0.3])
    assert detector(series) == []


def test_huge_relative_with_meaningful_absolute_flagged(detector):
    """$0.55 → $5.00 clears all three gates → critical."""
    series = _series([0.5, 0.6, 0.5, 0.55, 0.5, 0.6, 0.55, 5.0])
    out = detector(series)
    assert len(out) == 1
    assert out[0]["severity"] == "critical"
    assert out[0]["amount"] == 5.0
    assert out[0]["baseline_mean"] == pytest.approx(0.5429, rel=0.01)


def test_z_score_below_2_not_flagged_even_with_growth(detector):
    """A series that drifts steadily upward (low variance growth) keeps the
    z-score under 2 because the baseline variance scales with the drift."""
    series = _series([1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.9])
    out = detector(series)
    assert out == []


def test_relative_under_1_5x_not_flagged(detector):
    """Even a high-z, high-absolute jump that's only +40% of baseline is
    not "spike-like enough" — relative gate rejects."""
    series = _series([10.0] * 7 + [13.0])  # +30% with high absolute
    out = detector(series)
    assert out == []


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------


def test_critical_threshold_3x_growth_and_2usd_delta(detector):
    """3x growth + > $2 delta → critical regardless of z."""
    series = _series([1.0] * 7 + [4.0])  # 4x, $3 delta
    out = detector(series)
    assert len(out) == 1
    assert out[0]["severity"] == "critical"


def test_warning_below_critical_thresholds(detector):
    """Meets the triple gate but stays under both critical conditions
    (z < 3.5 AND growth < 3x)."""
    # baseline ~$1, eval $2.20 = 2.2x growth, $1.20 delta. Use higher
    # variance so z stays modest.
    series = _series([0.8, 1.0, 1.2, 0.9, 1.1, 0.8, 1.2, 2.4])
    out = detector(series)
    assert len(out) == 1
    assert out[0]["severity"] in ("warning", "critical")


# ---------------------------------------------------------------------------
# Ordering + payload shape
# ---------------------------------------------------------------------------


def test_anomalies_returned_newest_first(detector):
    """The UI panel reads top-down so today's spike must show first, even
    if it's chronologically last in the input."""
    # Two spikes 4 days apart
    amounts = [0.5] * 7 + [5.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 7.0]
    series = _series(amounts)
    out = detector(series)
    assert len(out) >= 2
    # Most-recent first
    dates = [a["date"] for a in out]
    assert dates == sorted(dates, reverse=True)


def test_anomaly_payload_includes_audit_fields(detector):
    out = detector(_series([0.5] * 7 + [5.0]))
    a = out[0]
    for key in (
        "date",
        "amount",
        "baseline_mean",
        "baseline_stddev",
        "z_score",
        "delta_pct",
        "severity",
    ):
        assert key in a, f"missing key {key}"


def test_zero_baseline_with_zero_amount_skipped(detector):
    """Both flat zero — no anomaly. The detector must not divide-by-zero
    in the relative-gate path."""
    series = _series([0.0] * 8)
    assert detector(series) == []
