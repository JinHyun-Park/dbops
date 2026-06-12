"""dynamodb_pricing — look up REAL DynamoDB capacity prices from the AWS Price
List API, mirroring aurora_pricing.py.

WHY: the capacity-mode cost simulator must compare Provisioned vs On-Demand with
the table's ACTUAL regional prices, not a hardcoded us-east-1 rate. A wrong
dollar number is worse than none, so every lookup fails soft → None and the
caller marks the result a fallback rather than fabricating a figure.

Four prices, all matched by usagetype SUFFIX so they stay region-agnostic (the
region prefix like APN2- varies). Two confounders MUST be excluded because they
share the same suffix:
  - "IA" (Infrequent Access table class): APN2-IA-ReadCapacityUnit-Hrs etc.
  - "Repl"/"ReplWrite" (global-table replicated writes): APN2-ReplWriteCapacityUnit-Hrs,
    APN2-ReplWriteRequestUnits — these end with the same WriteCapacityUnit-Hrs /
    WriteRequestUnits suffix, so suffix-match alone would mis-pick them.

Confirmed live against get_products(ServiceCode="AmazonDynamoDB",
regionCode="ap-northeast-2") on 2026-06-12:
  - APN2-ReadCapacityUnit-Hrs   (group DDB-ReadUnits)  $0.00014098 /RCU-hr
  - APN2-WriteCapacityUnit-Hrs  (group DDB-WriteUnits) $0.0007049  /WCU-hr
  - APN2-ReadRequestUnits   pricePerUnit $0.0000001355 → $0.1355 /million RRU
  - APN2-WriteRequestUnits  pricePerUnit $0.0000006800 → $0.68   /million WRU

The provisioned capacity-hour SKUs are TIERED (a $0 free-tier band for the first
25 units * 744h ≈ 18600, then the real rate beyond). We deliberately pick the
NON-ZERO (beyond-free-tier) price dimension so a continuously-running table is
priced at its marginal rate, never $0. The on-demand request-unit price is
published PER request unit (pricePerUnit ~1.4e-7); we normalize to $/million
internally (× 1e6) and label it clearly.
"""

import json

import boto3

# Process-level cache: {(kind, region): price_or_None}.
_CACHE: dict = {}

_MAX_PAGES = 8  # a DynamoDB region has a small SKU count; bound pagination latency.


def _client():
    # Price List API endpoints exist only in us-east-1 / ap-south-1 / eu-central-1.
    # The table's region is a FILTER value, not the client region.
    return boto3.client("pricing", region_name="us-east-1")


def _on_demand_usd(product: dict):
    """Pull the OnDemand USD price-per-unit out of a Price List product dict.

    DynamoDB capacity SKUs are flat-rate (one published price dimension beyond
    the free-tier band), so there is exactly one paid (non-zero) price dimension.
    DynamoDB provisioned capacity SKUs publish TWO OnDemand price dimensions: a
    $0 free-tier band and the real beyond-free-tier rate. Return the first
    NON-ZERO USD so a running table is priced at its marginal rate, not $0."""
    fallback_zero = None
    for term in product.get("terms", {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            usd = dim.get("pricePerUnit", {}).get("USD")
            if usd is None:
                continue
            try:
                val = float(usd)
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
            fallback_zero = val
    return fallback_zero


def _lookup(kind: str, region: str, suffix: str, exclude: tuple):
    """Page get_products for `region`, return the OnDemand USD of the product
    whose usagetype ends with `suffix` and contains none of `exclude`. None on
    any miss/error (caller marks a fallback). Cached per (kind, region)."""
    key = (kind, region)
    if key in _CACHE:
        return _CACHE[key]

    result = None
    try:
        pricing = _client()
        token = None
        pages = 0
        while pages < _MAX_PAGES:
            kwargs = {
                "ServiceCode": "AmazonDynamoDB",
                "Filters": [
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                ],
                "MaxResults": 100,
            }
            if token:
                kwargs["NextToken"] = token
            resp = pricing.get_products(**kwargs)
            for raw in resp.get("PriceList", []):
                product = json.loads(raw)
                ut = product.get("product", {}).get("attributes", {}).get("usagetype", "")
                if not ut.endswith(suffix):
                    continue
                if any(tok in ut for tok in exclude):
                    continue
                price = _on_demand_usd(product)
                if price is not None:
                    result = price
                    break
            if result is not None:
                break
            token = resp.get("NextToken")
            if not token:
                break
            pages += 1
    except Exception as e:  # pragma: no cover - network/permission/soft-fail
        print(f"[dynamodb_pricing] {kind} lookup failed ({region}): {e}")
        result = None

    _CACHE[key] = result
    return result


def price_per_rcu_hour(region: str):
    """$/provisioned-RCU-hour in `region`, or None. Standard table class only
    (excludes the IA and replicated-write confounders)."""
    return _lookup("rcu_hr", region, "ReadCapacityUnit-Hrs", ("IA-", "Repl"))


def price_per_wcu_hour(region: str):
    """$/provisioned-WCU-hour in `region`, or None. Standard table class only;
    excludes ReplWriteCapacityUnit-Hrs (shares the WriteCapacityUnit-Hrs suffix)
    and the IA variant."""
    return _lookup("wcu_hr", region, "WriteCapacityUnit-Hrs", ("IA-", "Repl"))


def price_per_million_rru(region: str):
    """$/million on-demand Read Request Units in `region`, or None. The API
    publishes per-request; we normalize to $/million (× 1e6)."""
    per_unit = _lookup("rru", region, "ReadRequestUnits", ("IA-", "Repl"))
    return per_unit * 1_000_000 if per_unit is not None else None


def price_per_million_wru(region: str):
    """$/million on-demand Write Request Units in `region`, or None. Excludes
    ReplWriteRequestUnits (shares the WriteRequestUnits suffix) and IA."""
    per_unit = _lookup("wru", region, "WriteRequestUnits", ("IA-", "Repl"))
    return per_unit * 1_000_000 if per_unit is not None else None
