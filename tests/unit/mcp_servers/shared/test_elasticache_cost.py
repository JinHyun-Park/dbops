import importlib.util
from pathlib import Path
from unittest.mock import patch

_C = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/shared/elasticache_cost.py"
_spec = importlib.util.spec_from_file_location("ec_cost", _C)
cost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cost)


def test_resize_cost_both_prices():
    with patch.object(cost, "price_per_node_hour", side_effect=lambda r, e, nt: {"cache.t4g.micro": 0.02, "cache.r7g.large": 0.30}[nt]):
        r = cost.compute_node_resize_cost("redis", "ap-northeast-2", "cache.t4g.micro", 1, "cache.r7g.large", 2)
    assert r["status"] == "ok"
    assert round(r["current_monthly"], 2) == round(0.02 * 1 * 730, 2)
    assert round(r["proposed_monthly"], 2) == round(0.30 * 2 * 730, 2)
    assert r["delta_monthly"] > 0


def test_resize_cost_partial_when_price_missing():
    with patch.object(cost, "price_per_node_hour", side_effect=lambda r, e, nt: 0.02 if nt == "cache.t4g.micro" else None):
        r = cost.compute_node_resize_cost("redis", "ap-northeast-2", "cache.t4g.micro", 1, "cache.x.unknown", 1)
    assert r["status"] == "partial"
    assert r["proposed_monthly"] is None
    assert r["current_monthly"] is not None
