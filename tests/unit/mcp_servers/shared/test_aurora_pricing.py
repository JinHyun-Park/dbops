"""Tests for the AWS Price List lookups — esp. that an unmatched edition does
NOT silently return the wrong-edition price (it must return None)."""
import json
from unittest.mock import MagicMock, patch

import mcp_servers.shared.aurora_pricing as ap


def _product(usagetype, usd):
    return json.dumps({
        "product": {"attributes": {"usagetype": usagetype}},
        "terms": {"OnDemand": {"x": {"priceDimensions": {"y": {"pricePerUnit": {"USD": str(usd)}}}}}},
    })


def _pricing_client(products):
    cli = MagicMock()
    cli.get_products.return_value = {"PriceList": products}
    return cli


def setup_function():
    ap._CACHE.clear()


@patch.object(ap, "_client")
def test_instance_picks_exact_edition(mock_client):
    mock_client.return_value = _pricing_client([
        _product("APN2-InstanceUsage:db.r6g.large", 0.313),
        _product("APN2-InstanceUsageIOOptimized:db.r6g.large", 0.407),
    ])
    assert ap.price_per_instance_hour("ap-northeast-2", "aurora-postgresql", "db.r6g.large", False) == 0.313
    ap._CACHE.clear()
    assert ap.price_per_instance_hour("ap-northeast-2", "aurora-postgresql", "db.r6g.large", True) == 0.407


@patch.object(ap, "_client")
def test_instance_no_matching_edition_returns_none_not_wrong_price(mock_client):
    # Only the Standard SKU exists; an I/O-Optimized request must NOT borrow it.
    mock_client.return_value = _pricing_client([
        _product("APN2-InstanceUsage:db.r4.large", 0.35),
    ])
    assert ap.price_per_instance_hour("ap-northeast-2", "aurora-postgresql", "db.r4.large", True) is None


@patch.object(ap, "_client")
def test_acu_suffix_match_standard_excludes_io(mock_client):
    mock_client.return_value = _pricing_client([
        _product("APN2-Aurora:ServerlessV2IOOptimizedUsage", 0.26),
        _product("APN2-Aurora:ServerlessV2Usage", 0.20),
    ])
    assert ap.price_per_acu_hour("ap-northeast-2", "aurora-postgresql", False) == 0.20
    ap._CACHE.clear()
    assert ap.price_per_acu_hour("ap-northeast-2", "aurora-postgresql", True) == 0.26
