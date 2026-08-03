import json
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Extended Support SKUs are a surcharge, not an alternative node rate
# ---------------------------------------------------------------------------


def _sku(usagetype, usd):
    """A Price List product in the shape get_products actually returns."""
    return json.dumps({
        "product": {"attributes": {"usagetype": usagetype}},
        "terms": {"OnDemand": {"t": {"priceDimensions": {"d": {
            "pricePerUnit": {"USD": str(usd)}}}}}},
    })


def test_an_extended_support_surcharge_is_never_returned_as_the_node_price(monkeypatch):
    """A node type returns SEVERAL SKUs and they are different CHARGES, not variants
    of one price. Measured 2026-08-03 for cache.t4g.micro / Redis / ap-northeast-2:

        $0.024  APN2-NodeUsage:cache.t4g.micro           <- the node price
        $0.019  APN2-ExtendedSupportYr1_Yr2-NodeUsage:   <- EOL surcharge
        $0.038  APN2-ExtendedSupportYr3-NodeUsage:       <- EOL surcharge

    The old loop took the first SKU carrying a price, so it could report a surcharge
    AS the node cost. The surcharge is deliberately listed FIRST here, which is the
    case the old code got wrong.
    """
    import mcp_servers.shared.elasticache_pricing as ep

    ep._CACHE.clear()
    client = MagicMock()
    client.get_products.return_value = {"PriceList": [
        _sku("APN2-ExtendedSupportYr1_Yr2-NodeUsage:cache.t4g.micro", 0.019),
        _sku("APN2-NodeUsage:cache.t4g.micro", 0.024),
        _sku("APN2-ExtendedSupportYr3-NodeUsage:cache.t4g.micro", 0.038),
    ]}
    monkeypatch.setattr(ep, "_client", lambda: client)
    assert ep.price_per_node_hour("ap-northeast-2", "redis", "cache.t4g.micro") == 0.024


def test_only_surcharge_skus_yields_a_miss_not_a_surcharge(monkeypatch):
    """If no node SKU exists there is no node price to report. Returning a surcharge
    is precisely the bug, so the honest answer is None and the caller's existing
    fallback path handles it."""
    import mcp_servers.shared.elasticache_pricing as ep

    ep._CACHE.clear()
    client = MagicMock()
    client.get_products.return_value = {"PriceList": [
        _sku("APN2-ExtendedSupportYr3-NodeUsage:cache.t4g.micro", 0.038),
    ]}
    monkeypatch.setattr(ep, "_client", lambda: client)
    assert ep.price_per_node_hour("ap-northeast-2", "redis", "cache.t4g.micro") is None


def test_valkey_queries_its_own_sku_not_the_redis_one(monkeypatch):
    """Valkey was aliased to "Redis". It has its own SKU, and it also has no
    extended-support SKUs yet, which is the only reason the alias looked harmless."""
    import mcp_servers.shared.elasticache_pricing as ep

    ep._CACHE.clear()
    client = MagicMock()
    client.get_products.return_value = {"PriceList": [
        _sku("APN2-NodeUsage:cache.t4g.micro", 0.0192)]}
    monkeypatch.setattr(ep, "_client", lambda: client)
    ep.price_per_node_hour("ap-northeast-2", "valkey", "cache.t4g.micro")
    engines = [f["Value"] for f in client.get_products.call_args.kwargs["Filters"]
               if f["Field"] == "cacheEngine"]
    assert engines == ["Valkey"]
