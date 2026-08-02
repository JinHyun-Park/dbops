"""elasticache_pricing — REAL ElastiCache node prices from the AWS Price List
API. No hardcoded prices (regional staleness). Target region is the `regionCode`
FILTER, not the client region. Process-cached, soft-fail (None on miss)."""

import json

import boto3

# Price List `cacheEngine` attribute values.
#
# Valkey used to be aliased to "Redis" with the comment "Valkey is priced as Redis
# today". That stopped being true: AWS publishes a separate Valkey SKU. Measured
# against the live Price List API on 2026-08-02 for cache.t4g.small in
# ap-northeast-2: cacheEngine=Valkey is $0.0376/hr while the Redis SKU this code
# picked is $0.047/hr, so every Valkey node was priced 25% high, and the response
# still stamped source=aws_price_list. The lookup soft-fails to None where a SKU is
# absent, so a region without Valkey SKUs degrades to the existing fallback rather
# than to a wrong number.
_ENGINE_LABEL = {"redis": "Redis", "valkey": "Valkey", "memcached": "Memcached"}

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
    except Exception as e:  # pragma: no cover
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
