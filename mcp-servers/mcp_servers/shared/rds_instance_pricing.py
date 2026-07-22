"""rds_instance_pricing — REAL RDS (non-Aurora) prices from the AWS Price List API.

RDS instances differ from Aurora: no I/O-Optimized variant, but a licenseModel /
edition dimension (SQL Server) and SEPARATE storage + provisioned-IOPS SKUs.
Every lookup fails soft (None / null fields) so a pricing outage degrades a
simulation to an estimate rather than breaking it. Prices are OnDemand,
Single-AZ unless multi_az=True.

Label strings below were confirmed against the live Price List API
(ServiceCode=AmazonRDS, regionCode=ap-northeast-2, instanceType=db.t3.small):
unlike Aurora, RDS's `databaseEngine` attribute does NOT vary by SQL Server
edition — it is the flat string "SQL Server" for Express/Web/Standard/
Enterprise alike. Edition is carried in a separate `databaseEdition`
attribute (seen values: "Express", "Web"; "Standard"/"Enterprise" inferred
from AWS's documented edition set, not directly observed in the probe
sample). `licenseModel` also differs by engine: RDS MySQL SKUs use
"No license required" (MySQL has no AWS license fee), while RDS SQL Server
SKUs use "License included" — a single hardcoded licenseModel value across
engines would silently return zero SKUs for MySQL.
"""

import json

import boto3

# Registry engine -> Price List `databaseEngine` attribute. Confirmed live:
# RDS SQL Server's databaseEngine is the same "SQL Server" string regardless
# of edition (see _RDS_EDITION_LABEL for the edition-discriminating filter).
RDS_ENGINE_LABEL = {
    "mysql": "MySQL",
    "sqlserver-ex": "SQL Server",
    "sqlserver-web": "SQL Server",
    "sqlserver-se": "SQL Server",
    "sqlserver-ee": "SQL Server",
}

# Registry engine -> Price List `databaseEdition` attribute (SQL Server only).
_RDS_EDITION_LABEL = {
    "sqlserver-ex": "Express",
    "sqlserver-web": "Web",
    "sqlserver-se": "Standard",
    "sqlserver-ee": "Enterprise",
}

# Registry engine -> Price List `licenseModel` attribute. Confirmed live:
# MySQL carries no license fee; SQL Server is License-included by default.
_RDS_LICENSE_MODEL = {
    "mysql": "No license required",
}
_DEFAULT_LICENSE_MODEL = "License included"

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


def _label(engine: str) -> str | None:
    return RDS_ENGINE_LABEL.get((engine or "").lower())


def price_rds_instance_hour(region, engine, instance_class, edition=None, multi_az=False):
    engine_key = (engine or "").lower()
    label = _label(engine_key)
    if not instance_class or not label:
        return None
    deploy = "Multi-AZ" if multi_az else "Single-AZ"
    key = ("inst", region, label, instance_class, deploy, edition)
    if key in _CACHE:
        return _CACHE[key]
    result = None
    try:
        filters = [
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
            {"Type": "TERM_MATCH", "Field": "databaseEngine", "Value": label},
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_class},
            {"Type": "TERM_MATCH", "Field": "deploymentOption", "Value": deploy},
            {"Type": "TERM_MATCH", "Field": "licenseModel",
             "Value": _RDS_LICENSE_MODEL.get(engine_key, _DEFAULT_LICENSE_MODEL)},
        ]
        # SQL Server: databaseEngine is edition-agnostic, so edition needs its
        # own filter. edition arg overrides the engine-derived default.
        edition_label = edition or _RDS_EDITION_LABEL.get(engine_key)
        if edition_label:
            filters.append({"Type": "TERM_MATCH", "Field": "databaseEdition", "Value": edition_label})
        resp = _client().get_products(
            ServiceCode="AmazonRDS",
            Filters=filters,
            MaxResults=100,
        )
        for raw in resp.get("PriceList", []):
            product = json.loads(raw)
            price = _on_demand_usd(product)
            if price is not None:
                result = price
                break
    except Exception as e:  # pragma: no cover - soft fail
        print(f"[rds_instance_pricing] instance lookup failed ({region}/{engine}/{instance_class}): {e}")
        result = None
    _CACHE[key] = result
    return result


def price_rds_storage_month(region, storage_type, gb, provisioned_iops=None):
    """gp3/gp2/io1/io2 storage + optional provisioned-IOPS monthly cost.
    Returns {"storage_usd": float|None, "iops_usd": float|None}. gp3 includes a
    3000-IOPS baseline: only IOPS above baseline is charged (0 below it)."""
    st = (storage_type or "gp3").lower()
    # volumeType is confirmed distinct per storage type (gp3 has its OWN
    # "General Purpose-GP3" label, separate from gp2's plain "General
    # Purpose" — mapping both to the same value would silently price gp3 at
    # the gp2 rate).
    vol_map = {
        "gp3": "General Purpose-GP3",
        "gp2": "General Purpose",
        "io1": "Provisioned IOPS",
        "io2": "Provisioned IOPS-IO2",
    }
    key = ("stor", region, st, round(float(gb or 0), 2), provisioned_iops)
    if key in _CACHE:
        return _CACHE[key]
    storage_usd = iops_usd = None
    try:
        cli = _client()
        # Storage GB-month
        resp = cli.get_products(
            ServiceCode="AmazonRDS",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                {"Type": "TERM_MATCH", "Field": "volumeType", "Value": vol_map.get(st, "General Purpose-GP3")},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Database Storage"},
            ],
            MaxResults=100,
        )
        for raw in resp.get("PriceList", []):
            unit = _on_demand_usd(json.loads(raw))
            if unit is not None:
                storage_usd = round(unit * float(gb or 0), 2)
                break
        # Provisioned IOPS (io1/io2, or gp3 above 3000 baseline)
        billable_iops = 0
        if st in ("io1", "io2") and provisioned_iops:
            billable_iops = provisioned_iops
        elif st == "gp3" and provisioned_iops and provisioned_iops > 3000:
            billable_iops = provisioned_iops - 3000
        if billable_iops:
            resp = cli.get_products(
                ServiceCode="AmazonRDS",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                    {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Provisioned IOPS"},
                ],
                MaxResults=100,
            )
            for raw in resp.get("PriceList", []):
                unit = _on_demand_usd(json.loads(raw))
                if unit is not None:
                    iops_usd = round(unit * billable_iops, 2)
                    break
        else:
            iops_usd = 0.0
    except Exception as e:  # pragma: no cover - soft fail
        print(f"[rds_instance_pricing] storage lookup failed ({region}/{st}): {e}")
    out = {"storage_usd": storage_usd, "iops_usd": iops_usd}
    _CACHE[key] = out
    return out
