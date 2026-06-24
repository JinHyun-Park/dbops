"""elasticache_pricing — REAL ElastiCache node prices from the AWS Price List
API (no hardcoded prices). Mirrors mcp-servers/shared/elasticache_pricing.py;
kept as a sibling module here so the Lambda asset stays self-contained.

Target region is the `regionCode` FILTER, not the client region. Process-cached,
soft-fail (returns None on any pricing miss)."""

import json

import boto3

# Price List `cacheEngine` attribute values. Valkey is priced as Redis today.
_ENGINE_LABEL = {"redis": "Redis", "valkey": "Redis", "memcached": "Memcached"}

_CACHE: dict = {}


def _client():
    return boto3.client("pricing", region_name="us-east-1")


def _on_demand_usd(product: dict):
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            usd = dim.get("pricePerUnit", {}).get("USD")
            if usd is not None:
                try:
                    return float(usd)
                except (TypeError, ValueError):
                    return None
    return None


def price_per_node_hour(region: str, engine: str, node_type: str):
    """$/hour for an ElastiCache node in `region`, or None if unavailable."""
    if not node_type:
        return None
    eng = (engine or "redis").lower()
    label = _ENGINE_LABEL.get(eng, "Redis")
    key = ("node", region, label, node_type)
    if key in _CACHE:
        return _CACHE[key]
    result = None
    try:
        resp = _client().get_products(
            ServiceCode="AmazonElastiCache",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": node_type},
                {"Type": "TERM_MATCH", "Field": "cacheEngine", "Value": label},
            ],
            MaxResults=100,
        )
    except Exception as e:
        print(f"[elasticache_pricing] lookup failed ({region}/{node_type}): {e}")
        _CACHE[key] = None
        return None
    for raw in resp.get("PriceList", []):
        product = json.loads(raw)
        price = _on_demand_usd(product)
        if price is not None:
            result = price
            break
    _CACHE[key] = result
    return result
