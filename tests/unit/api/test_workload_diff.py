"""Tests for the workload diff (pg_stat_statements snapshot delta)."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

_DASH = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASH))
_spec = importlib.util.spec_from_file_location("dashboard_handler", _DASH / "handler.py")
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _make_query(before_rows, after_rows):
    """The handler issues the same side_sql twice — first call is the
    `before` side, second is `after`. Return a query() stub that hands
    back each list in order."""
    calls = {"n": 0}

    def q(sql, params):
        calls["n"] += 1
        return before_rows if calls["n"] == 1 else after_rows

    return q


def _row(h, mean, calls=100, excerpt="SELECT 1"):
    return {
        "query_hash": h,
        "query_excerpt": excerpt,
        "calls": calls,
        "total_time_ms": mean * calls,
        "mean_time_ms": mean,
        "rows_returned": calls,
        "snapshot_time": "2026-05-20T00:00:00Z",
    }


def test_new_query_detected():
    q = _make_query(
        before_rows=[_row("a", 10)],
        after_rows=[_row("a", 10), _row("b", 50, excerpt="SELECT * FROM new_t")],
    )
    out = handler._workload_diff(q, "c1", "2026-05-19T00:00:00Z", "2026-05-20T00:00:00Z", 20.0, 120)
    assert out["totals"]["new"] == 1
    assert out["new"][0]["query_hash"] == "b"
    assert out["new"][0]["query_excerpt"] == "SELECT * FROM new_t"


def test_disappeared_query_detected():
    q = _make_query(
        before_rows=[_row("a", 10), _row("gone", 30)],
        after_rows=[_row("a", 10)],
    )
    out = handler._workload_diff(q, "c1", "t0", "t1", 20.0, 120)
    assert out["totals"]["disappeared"] == 1
    assert out["disappeared"][0]["query_hash"] == "gone"


def test_regressed_query_over_threshold():
    """mean 10 → 15 is +50%, over the 20% threshold → regressed."""
    q = _make_query(
        before_rows=[_row("a", 10)],
        after_rows=[_row("a", 15)],
    )
    out = handler._workload_diff(q, "c1", "t0", "t1", 20.0, 120)
    assert out["totals"]["regressed"] == 1
    assert out["regressed"][0]["delta_pct"] == 50.0
    assert out["totals"]["improved"] == 0


def test_improved_query_over_threshold():
    """mean 20 → 10 is -50% → improved bucket."""
    q = _make_query(
        before_rows=[_row("a", 20)],
        after_rows=[_row("a", 10)],
    )
    out = handler._workload_diff(q, "c1", "t0", "t1", 20.0, 120)
    assert out["totals"]["improved"] == 1
    assert out["totals"]["regressed"] == 0


def test_small_change_under_threshold_ignored():
    """mean 10 → 11 is +10%, under 20% → neither bucket."""
    q = _make_query(
        before_rows=[_row("a", 10)],
        after_rows=[_row("a", 11)],
    )
    out = handler._workload_diff(q, "c1", "t0", "t1", 20.0, 120)
    assert out["totals"]["regressed"] == 0
    assert out["totals"]["improved"] == 0


def test_zero_baseline_skipped():
    """A before-mean of 0 can't produce a ratio — skip rather than
    divide by zero."""
    q = _make_query(
        before_rows=[_row("a", 0)],
        after_rows=[_row("a", 50)],
    )
    out = handler._workload_diff(q, "c1", "t0", "t1", 20.0, 120)
    assert out["totals"]["regressed"] == 0
    assert out["totals"]["improved"] == 0


def test_regressed_sorted_worst_first():
    q = _make_query(
        before_rows=[_row("a", 10), _row("b", 10)],
        after_rows=[_row("a", 12), _row("b", 30)],  # +20% vs +200%
    )
    out = handler._workload_diff(q, "c1", "t0", "t1", 15.0, 120)
    deltas = [r["delta_pct"] for r in out["regressed"]]
    assert deltas == sorted(deltas, reverse=True)
    assert out["regressed"][0]["query_hash"] == "b"


def test_totals_distinct_counts():
    q = _make_query(
        before_rows=[_row("a", 10), _row("b", 10)],
        after_rows=[_row("a", 10), _row("c", 10), _row("d", 10)],
    )
    out = handler._workload_diff(q, "c1", "t0", "t1", 20.0, 120)
    assert out["totals"]["before_distinct_queries"] == 2
    assert out["totals"]["after_distinct_queries"] == 3


def test_methodology_note_present():
    q = _make_query([], [])
    out = handler._workload_diff(q, "c1", "t0", "t1", 20.0, 90)
    assert "cumulative" in out["methodology"]
    assert "90-minute" in out["methodology"]
