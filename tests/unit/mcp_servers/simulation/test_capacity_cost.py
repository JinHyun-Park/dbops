"""Tests for the DynamoDB capacity-mode cost simulator.

Covers the pure compute (on-demand vs provisioned math, p99 sizing,
recommendation direction, no-data floor, partial/fallback when a price is None)
AND the MCP tool impl wiring (cache reads + pricing → compute).
"""
import math
from unittest.mock import MagicMock, patch

from mcp_servers.shared.dynamodb_cost import (
    HOURS_PER_MONTH,
    MIN_DATAPOINTS,
    compute_capacity_cost,
)
from mcp_servers.shared.models import QueryResult

# Seoul prices (confirmed live).
PRICES = {"rcu_hr": 0.00014098, "wcu_hr": 0.0007049, "m_rru": 0.1355, "m_wru": 0.68}

MODULE = "mcp_servers.simulation.tools.capacity_cost"


def _consumed(datapoints=120, sum_rcu=720000.0, sum_wcu=360000.0,
              p99_rcu=6000.0, p99_wcu=3000.0):
    return {
        "datapoints": datapoints,
        "sum_rcu": sum_rcu,
        "sum_wcu": sum_wcu,
        "p99_rcu_per_min": p99_rcu,
        "p99_wcu_per_min": p99_wcu,
    }


# --- pure compute -----------------------------------------------------------


def test_on_demand_math_scales_window_to_month():
    """On-Demand monthly = (Σrcu/1e6·$/Mrru + Σwcu/1e6·$/Mwru)·(730/window)."""
    window = 2.0
    consumed = _consumed(sum_rcu=2_000_000.0, sum_wcu=1_000_000.0)
    r = compute_capacity_cost(
        cluster_id="ddb-x", billing_mode="PAY_PER_REQUEST", region="ap-northeast-2",
        window_hours=window, consumed=consumed, provisioned=None, prices=PRICES,
    )
    mf = HOURS_PER_MONTH / window
    expected = (2_000_000.0 * mf / 1e6) * 0.1355 + (1_000_000.0 * mf / 1e6) * 0.68
    assert r["status"] == "ok"
    assert r["on_demand_monthly_usd"] == round(expected, 2)
    # On-demand table: current cost == the on-demand figure.
    assert r["current_monthly_usd"] == r["on_demand_monthly_usd"]
    assert r["pricing_source"] == "aws_pricing_api"


def test_provisioned_sizing_uses_p99_per_second_over_headroom_ceil():
    """rcu_sized = ceil( p99(consumed/min)/60 / headroom ); cost uses 730h."""
    consumed = _consumed(p99_rcu=6000.0, p99_wcu=3000.0)  # 100/s and 50/s
    headroom = 0.70
    r = compute_capacity_cost(
        cluster_id="ddb-x", billing_mode="PROVISIONED", region="ap-northeast-2",
        window_hours=24.0, consumed=consumed,
        provisioned={"rcu": 120.0, "wcu": 60.0}, prices=PRICES, headroom=headroom,
    )
    rcu_sized = math.ceil((6000.0 / 60.0) / headroom)  # ceil(142.8) = 143
    wcu_sized = math.ceil((3000.0 / 60.0) / headroom)  # ceil(71.4) = 72
    assert r["sizing"]["rcu_per_sec"] == rcu_sized
    assert r["sizing"]["wcu_per_sec"] == wcu_sized
    assert r["sizing"]["basis"] == "p99"
    expected_prov = (
        rcu_sized * 0.00014098 * HOURS_PER_MONTH + wcu_sized * 0.0007049 * HOURS_PER_MONTH
    )
    assert r["provisioned_monthly_usd"] == round(expected_prov, 2)
    # current cost from the REAL provisioned units (120/60), not the sized values.
    expected_cur = 120.0 * 0.00014098 * HOURS_PER_MONTH + 60.0 * 0.0007049 * HOURS_PER_MONTH
    assert r["current_monthly_usd"] == round(expected_cur, 2)


def test_provisioned_sizing_floors_at_one_unit_per_side():
    """A near-idle table (p99≈0) still needs the 1 RCU + 1 WCU provisioned minimum
    — sizing must floor at 1, not 0, so the provisioned estimate is the real $0.62-
    ish minimum and the recommendation isn't the degenerate 'Provisioned, save 100%'
    against an on-demand cost of ~$0."""
    consumed = _consumed(datapoints=120, sum_rcu=0.0, sum_wcu=0.0, p99_rcu=0.0, p99_wcu=0.0)
    r = compute_capacity_cost(
        cluster_id="ddb-idle", billing_mode="PAY_PER_REQUEST", region="ap-northeast-2",
        window_hours=168.0, consumed=consumed, provisioned=None, prices=PRICES,
    )
    assert r["sizing"]["rcu_per_sec"] == 1
    assert r["sizing"]["wcu_per_sec"] == 1
    expected_prov = 1 * 0.00014098 * HOURS_PER_MONTH + 1 * 0.0007049 * HOURS_PER_MONTH
    assert r["provisioned_monthly_usd"] == round(expected_prov, 2)
    assert r["provisioned_monthly_usd"] > 0
    # Idle table: on-demand (~$0) is cheaper than the 1+1 provisioned minimum.
    assert r["recommended_mode"] == "PAY_PER_REQUEST"


def test_recommendation_points_to_cheaper_mode():
    """A spiky-but-low-average table is cheaper on-demand → recommend on-demand."""
    # Low total consumption (cheap on-demand) but a high p99 spike (expensive
    # provisioned sizing) → on-demand wins.
    consumed = _consumed(sum_rcu=100_000.0, sum_wcu=50_000.0, p99_rcu=60000.0, p99_wcu=30000.0)
    r = compute_capacity_cost(
        cluster_id="ddb-x", billing_mode="PROVISIONED", region="ap-northeast-2",
        window_hours=168.0, consumed=consumed,
        provisioned={"rcu": 1000.0, "wcu": 500.0}, prices=PRICES,
    )
    assert r["recommended_mode"] == "PAY_PER_REQUEST"
    assert r["on_demand_monthly_usd"] < r["provisioned_monthly_usd"]
    assert r["monthly_savings_usd"] > 0
    assert 0 < r["savings_pct"] <= 100


def test_recommendation_prefers_provisioned_for_steady_heavy_load():
    """A steady heavy table (high sustained consumption) is cheaper provisioned."""
    # High total (expensive on-demand) with a modest p99 (cheap provisioned).
    consumed = _consumed(sum_rcu=500_000_000.0, sum_wcu=250_000_000.0,
                         p99_rcu=6000.0, p99_wcu=3000.0)
    r = compute_capacity_cost(
        cluster_id="ddb-x", billing_mode="PAY_PER_REQUEST", region="ap-northeast-2",
        window_hours=168.0, consumed=consumed, provisioned=None, prices=PRICES,
    )
    assert r["recommended_mode"] == "PROVISIONED"
    assert r["provisioned_monthly_usd"] < r["on_demand_monthly_usd"]


def test_no_data_floor():
    """< MIN_DATAPOINTS consumed rows → no_data + reason, no fabricated figures."""
    consumed = _consumed(datapoints=MIN_DATAPOINTS - 1)
    r = compute_capacity_cost(
        cluster_id="ddb-x", billing_mode="PROVISIONED", region="ap-northeast-2",
        window_hours=24.0, consumed=consumed, provisioned={"rcu": 10.0, "wcu": 5.0},
        prices=PRICES,
    )
    assert r["status"] == "no_data"
    assert r["no_data_reason"]
    assert "on_demand_monthly_usd" not in r  # no figures fabricated


def test_partial_when_a_price_is_none():
    """A missing on-demand price → partial/fallback, that side omitted (None), no
    recommendation, but the resolvable provisioned side still computes."""
    prices = {**PRICES, "m_rru": None}  # one on-demand price unresolved
    r = compute_capacity_cost(
        cluster_id="ddb-x", billing_mode="PROVISIONED", region="ap-northeast-2",
        window_hours=24.0, consumed=_consumed(),
        provisioned={"rcu": 100.0, "wcu": 50.0}, prices=prices,
    )
    assert r["status"] == "partial"
    assert r["pricing_source"] == "fallback"
    assert r["on_demand_monthly_usd"] is None  # never fabricated
    assert r["provisioned_monthly_usd"] is not None
    assert r["recommended_mode"] is None  # only when BOTH resolve
    assert r["monthly_savings_usd"] is None


# --- MCP tool impl wiring ---------------------------------------------------


def _cache_for(meta_row, consumed_row, provisioned_rows):
    """A CacheClient mock whose .execute(...).rows depends on the SQL text:
    meta (cluster_meta) / consumed agg / provisioned latest."""
    cache = MagicMock()

    def _exec(sql, params=None):
        if "cluster_meta" in sql:
            return QueryResult(columns=[], rows=[meta_row] if meta_row else [], row_count=1)
        if "percentile_cont" in sql:
            return QueryResult(columns=[], rows=[consumed_row], row_count=1)
        if "provisioned_rcu" in sql:
            return QueryResult(columns=[], rows=provisioned_rows, row_count=len(provisioned_rows))
        return QueryResult(columns=[], rows=[], row_count=0)

    cache.execute.side_effect = _exec
    return cache


def test_impl_provisioned_table_end_to_end():
    from mcp_servers.simulation.tools.capacity_cost import (
        simulate_dynamodb_capacity_cost_impl,
    )
    cache = _cache_for(
        meta_row={"region": "ap-northeast-2", "billing_mode": "PROVISIONED"},
        consumed_row={"datapoints": 120, "sum_rcu": 720000.0, "sum_wcu": 360000.0,
                      "p99_rcu": 6000.0, "p99_wcu": 3000.0},
        provisioned_rows=[
            {"metric_type": "provisioned_rcu", "value": 150.0},
            {"metric_type": "provisioned_wcu", "value": 75.0},
        ],
    )
    with patch(f"{MODULE}.price_per_rcu_hour", return_value=0.00014098), \
         patch(f"{MODULE}.price_per_wcu_hour", return_value=0.0007049), \
         patch(f"{MODULE}.price_per_million_rru", return_value=0.1355), \
         patch(f"{MODULE}.price_per_million_wru", return_value=0.68):
        r = simulate_dynamodb_capacity_cost_impl(cache, cluster_id="ddb-abc")

    assert r["status"] == "ok"
    assert r["billing_mode"] == "PROVISIONED"
    assert r["region"] == "ap-northeast-2"
    assert r["on_demand_monthly_usd"] is not None
    assert r["provisioned_monthly_usd"] is not None
    assert r["current_monthly_usd"] is not None
    assert r["recommended_mode"] in ("PROVISIONED", "PAY_PER_REQUEST")


def test_impl_pricing_miss_marks_fallback():
    from mcp_servers.simulation.tools.capacity_cost import (
        simulate_dynamodb_capacity_cost_impl,
    )
    cache = _cache_for(
        meta_row={"region": "ap-northeast-2", "billing_mode": "PAY_PER_REQUEST"},
        consumed_row={"datapoints": 120, "sum_rcu": 720000.0, "sum_wcu": 360000.0,
                      "p99_rcu": 6000.0, "p99_wcu": 3000.0},
        provisioned_rows=[],
    )
    with patch(f"{MODULE}.price_per_rcu_hour", return_value=None), \
         patch(f"{MODULE}.price_per_wcu_hour", return_value=None), \
         patch(f"{MODULE}.price_per_million_rru", return_value=None), \
         patch(f"{MODULE}.price_per_million_wru", return_value=None):
        r = simulate_dynamodb_capacity_cost_impl(cache, cluster_id="ddb-abc")

    assert r["status"] == "partial"
    assert r["pricing_source"] == "fallback"
    assert r["on_demand_monthly_usd"] is None
    assert r["provisioned_monthly_usd"] is None
    assert r["recommended_mode"] is None


# --- unsupported table class / global table ---------------------------------


def test_unsupported_ia_table_class():
    """STANDARD_INFREQUENT_ACCESS table class → status "unsupported", no dollars."""
    consumed = _consumed()  # datapoints=120, well above MIN_DATAPOINTS
    r = compute_capacity_cost(
        cluster_id="ddb-ia",
        billing_mode="PAY_PER_REQUEST",
        region="ap-northeast-2",
        window_hours=168.0,
        consumed=consumed,
        provisioned=None,
        prices=PRICES,
        table_class="STANDARD_INFREQUENT_ACCESS",
    )
    assert r["status"] == "unsupported"
    assert r["unsupported_reason"]
    assert "STANDARD_INFREQUENT_ACCESS" in r["unsupported_reason"]
    assert r["on_demand_monthly_usd"] is None
    assert r["provisioned_monthly_usd"] is None
    assert r["current_monthly_usd"] is None
    assert r["recommended_mode"] is None
    assert r["monthly_savings_usd"] is None
    assert r["savings_pct"] is None


def test_unsupported_global_table():
    """Global table (is_global_table=True) → status "unsupported", no dollars."""
    consumed = _consumed()
    r = compute_capacity_cost(
        cluster_id="ddb-global",
        billing_mode="PROVISIONED",
        region="us-east-1",
        window_hours=168.0,
        consumed=consumed,
        provisioned={"rcu": 100.0, "wcu": 50.0},
        prices=PRICES,
        table_class="STANDARD",
        is_global_table=True,
    )
    assert r["status"] == "unsupported"
    assert r["unsupported_reason"]
    assert "글로벌" in r["unsupported_reason"]
    assert r["on_demand_monthly_usd"] is None
    assert r["recommended_mode"] is None


def test_standard_non_global_still_computes():
    """Explicit table_class="STANDARD", is_global_table=False → normal path unchanged."""
    consumed = _consumed()
    r = compute_capacity_cost(
        cluster_id="ddb-std",
        billing_mode="PAY_PER_REQUEST",
        region="ap-northeast-2",
        window_hours=168.0,
        consumed=consumed,
        provisioned=None,
        prices=PRICES,
        table_class="STANDARD",
        is_global_table=False,
    )
    assert r["status"] == "ok"
    assert r["on_demand_monthly_usd"] is not None
    assert r["provisioned_monthly_usd"] is not None
    assert r["recommended_mode"] in ("PROVISIONED", "PAY_PER_REQUEST")
