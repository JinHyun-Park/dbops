"""aurora_pricing — look up REAL Aurora prices from the AWS Price List API.

WHY: simulations must not hardcode a single region/edition price (the old
scaling tool baked in a us-east-1 Standard ACU rate of $0.12, which is wrong
almost everywhere — Seoul I/O-Optimized is $0.26). This resolves the actual
$/hour for a cluster's region, engine, edition (Standard vs I/O-Optimized),
and either its Serverless v2 ACU rate or a provisioned instance class.

The Price List API ("pricing") is only served from a few endpoints; us-east-1
is always valid, and the TARGET region is passed as the `regionCode` FILTER —
NOT the client region. Results are cached per process and every lookup fails
soft (returns None) so a pricing outage degrades a simulation to an estimate
rather than breaking it.
"""

import json

import boto3

# Map RDS engine identifiers to the Price List `databaseEngine` attribute.
_ENGINE_LABEL = {
    "aurora-postgresql": "Aurora PostgreSQL",
    "aurora-mysql": "Aurora MySQL",
}

# Process-level cache: {(kind, region, engine, io_opt, key): price_or_None}.
_CACHE: dict = {}

_MAX_PAGES = 12  # bound ACU pagination latency; an RDS region has a few hundred SKUs.


def _client():
    # Price List API endpoints exist only in us-east-1 / ap-south-1 / eu-central-1.
    # The cluster's region is a FILTER value, not the client region.
    return boto3.client("pricing", region_name="us-east-1")


def _engine_label(engine: str):
    """The Price List `databaseEngine` value, or None when this is not an Aurora
    engine we can price.

    This used to default to "Aurora PostgreSQL" for ANY unrecognised engine, which
    turns a miss into a confidently-priced wrong product. Measured 2026-08-02: a
    DocumentDB cluster reached this helper (its family gate was dead) and came back
    priced at the Aurora PostgreSQL rate with `source: "aws_price_list"` attached,
    i.e. a guess wearing the label of a measurement.

    Returning None is safe because every caller already handles a pricing miss:
    they null the cost fields and set source to "fallback". A missing price is
    honest; a wrong one that claims provenance is not.
    """
    return _ENGINE_LABEL.get((engine or "").lower())


def _on_demand_usd(product: dict):
    """Pull the OnDemand USD price-per-unit out of a Price List product dict."""
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            usd = dim.get("pricePerUnit", {}).get("USD")
            if usd is not None:
                try:
                    return float(usd)
                except (TypeError, ValueError):
                    return None
    return None


def price_per_acu_hour(region: str, engine: str, io_optimized: bool):
    """$/ACU-hour for Aurora Serverless v2 in `region`, or None if unavailable.

    Serverless v2 ACU products carry no instanceType, so they can't be filtered
    server-side beyond region+engine; we page and match the usagetype suffix
    (`ServerlessV2IOOptimizedUsage` vs `ServerlessV2Usage`). The region prefix
    on usagetype (e.g. APN2-) varies, so we match the suffix to stay region-
    agnostic — no hardcoded region codes."""
    key = ("acu", region, engine, bool(io_optimized), "")
    if key in _CACHE:
        return _CACHE[key]

    label = _engine_label(engine)
    if label is None:
        # Not an Aurora engine we can price. Return the miss BEFORE calling the API:
        # passing None as a TERM_MATCH value would raise inside the try below and
        # degrade to the same None via an exception, logging a spurious failure for
        # what is a known, expected non-match.
        _CACHE[key] = None
        return None
    suffix = "ServerlessV2IOOptimizedUsage" if io_optimized else "ServerlessV2Usage"
    result = None
    try:
        pricing = _client()
        token = None
        pages = 0
        while pages < _MAX_PAGES:
            kwargs = {
                "ServiceCode": "AmazonRDS",
                "Filters": [
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                    {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": label},
                ],
                "MaxResults": 100,
            }
            if token:
                kwargs["NextToken"] = token
            resp = pricing.get_products(**kwargs)
            for raw in resp.get("PriceList", []):
                product = json.loads(raw)
                ut = product.get("product", {}).get("attributes", {}).get("usagetype", "")
                # Standard suffix is also a suffix of the IOOptimized one, so for
                # the Standard case explicitly exclude the IO-Optimized variant.
                if ut.endswith(suffix) and (io_optimized or "IOOptimized" not in ut):
                    result = _on_demand_usd(product)
                    break
            if result is not None:
                break
            token = resp.get("NextToken")
            if not token:
                break
            pages += 1
    except Exception as e:  # pragma: no cover - network/permission/soft-fail
        print(f"[aurora_pricing] ACU lookup failed ({region}/{engine}/io={io_optimized}): {e}")
        result = None

    _CACHE[key] = result
    return result


def price_per_instance_hour(region: str, engine: str, instance_class: str, io_optimized: bool):
    """$/hour for a provisioned Aurora instance class in `region`, or None.

    Filtering by instanceType returns just the Standard + I/O-Optimized SKUs for
    that class; we pick by usagetype (`InstanceUsageIOOptimized` vs plain
    `InstanceUsage`)."""
    if not instance_class:
        return None
    key = ("instance", region, engine, bool(io_optimized), instance_class)
    if key in _CACHE:
        return _CACHE[key]

    label = _engine_label(engine)
    if label is None:
        # Not an Aurora engine we can price. Return the miss BEFORE calling the API:
        # passing None as a TERM_MATCH value would raise inside the try below and
        # degrade to the same None via an exception, logging a spurious failure for
        # what is a known, expected non-match.
        _CACHE[key] = None
        return None
    result = None
    try:
        pricing = _client()
        resp = pricing.get_products(
            ServiceCode="AmazonRDS",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": label},
                {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
            ],
            MaxResults=100,
        )
    except Exception as e:  # pragma: no cover
        print(f"[aurora_pricing] instance lookup failed ({region}/{instance_class}): {e}")
        _CACHE[key] = None
        return None

    for raw in resp.get("PriceList", []):
        product = json.loads(raw)
        ut = product.get("product", {}).get("attributes", {}).get("usagetype", "")
        price = _on_demand_usd(product)
        if price is None:
            continue
        is_io = "InstanceUsageIOOptimized" in ut
        # Match the EXACT edition only. We deliberately do NOT fall back to the
        # other edition's price: silently pricing an I/O-Optimized cluster at the
        # Standard rate (or vice versa) and reporting it as real pricing would be
        # misleading. No exact match -> None, so the caller marks it a fallback.
        if io_optimized and is_io:
            result = price
            break
        if not io_optimized and not is_io:
            result = price
            break
    _CACHE[key] = result
    return result
