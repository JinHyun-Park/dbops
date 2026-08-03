"""elasticache_pricing — REAL ElastiCache node prices from the AWS Price List
API. No hardcoded prices (regional staleness). Target region is the `regionCode`
FILTER, not the client region. Process-cached, soft-fail (None on miss)."""

import json

import boto3

# Price List `cacheEngine` attribute values.
#
# Valkey used to be aliased to "Redis" with the comment "Valkey is priced as Redis
# today". That stopped being true: AWS publishes a separate Valkey SKU. Measured
# against the live Price List API on 2026-08-02 in ap-northeast-2:
#
#   node             Valkey     Redis (all SKUs returned)
#   cache.t4g.small  0.0376     0.047, 0.075, 0.038
#   cache.t4g.micro  0.0192     0.019, 0.024, 0.038
#
# The error the alias caused was NOT a constant markup. Redis returns SEVERAL SKUs
# per node type and the loop below takes the FIRST one that carries a price, so the
# Valkey figure was off by an amount and a DIRECTION that depended on node type and
# on API ordering: +25% on t4g.small, -1% on t4g.micro. Querying the Valkey SKU
# makes it exact and deterministic instead.
#
# The multi-SKU ambiguity for actual Redis clusters is a SEPARATE, unfixed problem
# (0.019 to 0.038 for one node type) and is recorded in BACKLOG. The lookup
# soft-fails to None where a SKU is absent, so a region with no Valkey SKUs
# degrades to the existing fallback rather than to a wrong number.
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
    # PICK THE NODE SKU, do not take whichever came back first.
    #
    # A node type returns SEVERAL SKUs and they are not variants of one price, they
    # are different CHARGES. Measured 2026-08-03 for cache.t4g.micro / Redis /
    # ap-northeast-2:
    #
    #   $0.024  usagetype APN2-NodeUsage:cache.t4g.micro          <- the node price
    #   $0.019  usagetype APN2-ExtendedSupportYr1_Yr2-NodeUsage:  <- EOL surcharge
    #   $0.038  usagetype APN2-ExtendedSupportYr3-NodeUsage:      <- EOL surcharge
    #
    # Extended Support is an ADD-ON for running an engine version past end of
    # standard support, not an alternative node rate. The old `first price wins`
    # loop could therefore report a surcharge AS the node cost: $0.019 where the
    # node actually costs $0.024, and for cache.t4g.small the spread was 0.038 to
    # 0.075. Valkey looks correct today only because it is new enough to have no
    # extended-support SKUs yet, so this would have started lying about Valkey too.
    #
    # The real node SKU is the one whose usagetype has no ExtendedSupport segment.
    # `surcharges` is returned to the caller rather than discarded, because a
    # cluster on an EOL version really does pay them and silently dropping them
    # would understate its bill.
    surcharges = []
    for raw in resp.get("PriceList", []):
        product = json.loads(raw)
        price = _on_demand_usd(product)
        if price is None:
            continue
        usagetype = product.get("product", {}).get("attributes", {}).get("usagetype", "")
        if "ExtendedSupport" in usagetype:
            surcharges.append((usagetype, price))
            continue
        if result is None:
            result = price
    if result is None and surcharges:
        # Every SKU was a surcharge, so there is no node price to report. Returning
        # a surcharge here is exactly the bug above; report the miss instead.
        print(f"[elasticache_pricing] only ExtendedSupport SKUs for {region}/{node_type}/{label}")
    _CACHE[key] = result
    return result
