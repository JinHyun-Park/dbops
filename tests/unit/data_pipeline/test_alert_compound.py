"""Unit tests for the alert evaluator's compound AND/OR DSL.

The compound rules layer was added on top of the legacy single-threshold
path. These tests make sure:
  - Each operand resolves correctly against per-metric / per-window data
  - AND requires all operands to fire, OR requires any
  - "no data" doesn't accidentally count as a match
  - Aggregator selection (max / min / avg / last) maps to the right SQL
  - Legacy paths still work when `conditions` is absent

We mock the SQL fetch callback so the tests stay pure logic — no RDS."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "data-pipeline" / "alert_evaluator" / "handler.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "alert_evaluator_compound", HANDLER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["alert_evaluator_compound"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def h():
    return _load()


def _stub_query(returns_by_metric: dict[str, float | None]):
    """Build a fake query_fn that returns a single-row aggregate result
    keyed by which metric_type the SQL is asking for."""

    def q(_sql: str, params: dict):
        m = params.get("mt")
        v = returns_by_metric.get(m)
        return [{"v": v}] if v is not None else []

    return q


# ---------------------------------------------------------------------------
# Single-operand resolution
# ---------------------------------------------------------------------------


def test_operand_no_data_does_not_match(h):
    """Absent metric data must never produce a fire — explicit no-match,
    not "true because the absence is also not a fail"."""
    q = _stub_query({})
    matched, obs, summary = h._evaluate_operand(
        q,
        "prod-pg",
        {"metric_type": "cpu", "comparison": ">", "threshold": 80},
    )
    assert matched is False
    assert obs is None
    assert "no data" in summary


def test_operand_basic_threshold_above(h):
    q = _stub_query({"cpu": 92.5})
    matched, obs, summary = h._evaluate_operand(
        q,
        "prod-pg",
        {"metric_type": "cpu", "comparison": ">", "threshold": 80},
    )
    assert matched is True
    assert obs == pytest.approx(92.5)
    assert "cpu" in summary and ">" in summary


def test_operand_below_threshold(h):
    q = _stub_query({"cpu": 50})
    matched, _, _ = h._evaluate_operand(
        q,
        "prod-pg",
        {"metric_type": "cpu", "comparison": ">", "threshold": 80},
    )
    assert matched is False


def test_operand_invalid_comparison_rejected(h):
    """A rule with garbage comparison must not silently match — we
    return False with an "invalid operand" summary."""
    q = _stub_query({"cpu": 100})
    matched, _, summary = h._evaluate_operand(
        q,
        "prod-pg",
        {"metric_type": "cpu", "comparison": "approximately", "threshold": 80},
    )
    assert matched is False
    assert "invalid" in summary


# ---------------------------------------------------------------------------
# AND / OR composition
# ---------------------------------------------------------------------------


def test_compound_and_both_must_fire(h):
    """AND: both operands above their thresholds → overall True."""
    q = _stub_query({"cpu": 95, "db_connections": 150})
    matched, summaries = h._evaluate_conditions(
        q,
        "prod-pg",
        {
            "logic": "and",
            "operands": [
                {"metric_type": "cpu", "comparison": ">", "threshold": 80},
                {"metric_type": "db_connections", "comparison": ">", "threshold": 100},
            ],
        },
    )
    assert matched is True
    assert len(summaries) == 2


def test_compound_and_one_below_does_not_fire(h):
    q = _stub_query({"cpu": 95, "db_connections": 20})
    matched, _ = h._evaluate_conditions(
        q,
        "prod-pg",
        {
            "logic": "and",
            "operands": [
                {"metric_type": "cpu", "comparison": ">", "threshold": 80},
                {"metric_type": "db_connections", "comparison": ">", "threshold": 100},
            ],
        },
    )
    assert matched is False


def test_compound_or_any_fires(h):
    """OR: even if one operand is below, the other above suffices."""
    q = _stub_query({"cpu": 30, "db_connections": 200})
    matched, _ = h._evaluate_conditions(
        q,
        "prod-pg",
        {
            "logic": "or",
            "operands": [
                {"metric_type": "cpu", "comparison": ">", "threshold": 80},
                {"metric_type": "db_connections", "comparison": ">", "threshold": 100},
            ],
        },
    )
    assert matched is True


def test_compound_or_all_below_does_not_fire(h):
    q = _stub_query({"cpu": 30, "db_connections": 50})
    matched, _ = h._evaluate_conditions(
        q,
        "prod-pg",
        {
            "logic": "or",
            "operands": [
                {"metric_type": "cpu", "comparison": ">", "threshold": 80},
                {"metric_type": "db_connections", "comparison": ">", "threshold": 100},
            ],
        },
    )
    assert matched is False


def test_default_logic_is_and(h):
    """Missing/empty `logic` field must default to AND, not OR — failing
    safe (requires all conditions) instead of failing loud."""
    q = _stub_query({"cpu": 95, "db_connections": 20})  # only one fires
    matched, _ = h._evaluate_conditions(
        q,
        "prod-pg",
        {
            "operands": [
                {"metric_type": "cpu", "comparison": ">", "threshold": 80},
                {"metric_type": "db_connections", "comparison": ">", "threshold": 100},
            ],
        },
    )
    assert matched is False  # default AND would require both


def test_empty_operands_does_not_fire(h):
    matched, summaries = h._evaluate_conditions(
        _stub_query({}), "prod-pg", {"logic": "and", "operands": []}
    )
    assert matched is False
    assert "no operands" in summaries[0]


def test_no_data_for_one_operand_in_and_does_not_fire(h):
    """AND with one operand having no data and another firing must NOT
    fire — incomplete signal."""
    q = _stub_query({"cpu": 95})  # db_connections absent
    matched, _ = h._evaluate_conditions(
        q,
        "prod-pg",
        {
            "logic": "and",
            "operands": [
                {"metric_type": "cpu", "comparison": ">", "threshold": 80},
                {"metric_type": "db_connections", "comparison": ">", "threshold": 100},
            ],
        },
    )
    assert matched is False


def test_no_data_for_one_operand_in_or_does_fire_if_other_fires(h):
    """OR with one firing and one absent — the firing one wins, total True."""
    q = _stub_query({"cpu": 95})
    matched, _ = h._evaluate_conditions(
        q,
        "prod-pg",
        {
            "logic": "or",
            "operands": [
                {"metric_type": "cpu", "comparison": ">", "threshold": 80},
                {"metric_type": "db_connections", "comparison": ">", "threshold": 100},
            ],
        },
    )
    assert matched is True


# ---------------------------------------------------------------------------
# Aggregator mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agg, expected_sql_fn",
    [
        ("max", "MAX"),
        ("min", "MIN"),
        ("avg", "AVG"),
        ("last", "MAX"),  # 'last' approximates via MAX(ts)
        (None, "MAX"),  # default
        ("garbage", "MAX"),  # invalid falls back to MAX
    ],
)
def test_aggregator_maps_to_sql(h, agg, expected_sql_fn):
    captured = {}

    def fake_q(sql, _params):
        captured["sql"] = sql
        return [{"v": 0}]

    op = {"metric_type": "cpu", "comparison": ">", "threshold": 0}
    if agg is not None:
        op["agg"] = agg
    h._evaluate_operand(fake_q, "prod-pg", op)
    assert f"{expected_sql_fn}(value)" in captured["sql"]


def test_window_minutes_passed_to_query(h):
    captured = {}

    def fake_q(_sql, params):
        captured.update(params)
        return [{"v": 0}]

    h._evaluate_operand(
        fake_q,
        "prod-pg",
        {
            "metric_type": "cpu",
            "comparison": ">",
            "threshold": 0,
            "window_minutes": 30,
        },
    )
    assert captured["win"] == "30"


def test_default_window_minutes_is_10(h):
    """Backward compat: an old rule with no window_minutes must still
    behave like the legacy 10-minute lookback."""
    captured = {}

    def fake_q(_sql, params):
        captured.update(params)
        return [{"v": 0}]

    h._evaluate_operand(
        fake_q,
        "prod-pg",
        {"metric_type": "cpu", "comparison": ">", "threshold": 0},
    )
    assert captured["win"] == "10"
