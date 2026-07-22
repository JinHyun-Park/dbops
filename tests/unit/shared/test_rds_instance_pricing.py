import json
from unittest.mock import MagicMock, patch

from mcp_servers.shared import rds_instance_pricing as rp


def _product(usagetype, usd, db_engine, license_model="License included", deploy="Single-AZ"):
    return json.dumps({
        "product": {"attributes": {
            "usagetype": usagetype, "databaseEngine": db_engine,
            "licenseModel": license_model, "deploymentOption": deploy}},
        "terms": {"OnDemand": {"x": {"priceDimensions": {"y": {
            "pricePerUnit": {"USD": str(usd)}}}}}},
    })


def test_mysql_instance_hour_resolves():
    fake = MagicMock()
    fake.get_products.return_value = {"PriceList": [
        _product("APN2-InstanceUsage:db.t3.small", 0.045, "MySQL")]}
    with patch.object(rp, "_client", return_value=fake):
        rp._CACHE.clear()
        assert rp.price_rds_instance_hour("ap-northeast-2", "mysql", "db.t3.small") == 0.045


def test_sqlserver_express_uses_express_label():
    fake = MagicMock()
    # Only an Express SKU is returned; a wrong label would miss it.
    fake.get_products.return_value = {"PriceList": [
        _product("APN2-InstanceUsage:db.t3.small", 0.052,
                 rp.RDS_ENGINE_LABEL["sqlserver-ex"])]}
    with patch.object(rp, "_client", return_value=fake):
        rp._CACHE.clear()
        price = rp.price_rds_instance_hour("ap-northeast-2", "sqlserver-ex", "db.t3.small")
        assert price == 0.052
    # databaseEngine is the flat "SQL Server" for every edition, so it does NOT
    # discriminate — the edition MUST be carried by a separate databaseEdition
    # filter. Assert both: the flat engine label AND the Express edition filter.
    sent = fake.get_products.call_args.kwargs["Filters"]
    assert any(f["Field"] == "databaseEngine" and f["Value"] == rp.RDS_ENGINE_LABEL["sqlserver-ex"] for f in sent)
    assert any(f["Field"] == "databaseEdition" and f["Value"] == "Express" for f in sent)


def test_sqlserver_editions_do_not_share_cache_key():
    """Regression: databaseEngine is "SQL Server" for all editions, so the cache
    key must include the edition — else the second edition priced silently
    returns the first edition's cached price."""
    fake = MagicMock()

    def products(**kw):
        fields = {f["Field"]: f["Value"] for f in kw["Filters"]}
        price = {"Express": 0.052, "Enterprise": 0.601}.get(fields.get("databaseEdition"), 0.0)
        return {"PriceList": [_product("APN2-InstanceUsage:db.t3.small", price, "SQL Server")]}

    fake.get_products.side_effect = products
    with patch.object(rp, "_client", return_value=fake):
        rp._CACHE.clear()
        ex = rp.price_rds_instance_hour("ap-northeast-2", "sqlserver-ex", "db.t3.small")
        ee = rp.price_rds_instance_hour("ap-northeast-2", "sqlserver-ee", "db.t3.small")
    assert ex == 0.052
    assert ee == 0.601  # must NOT collapse onto the cached Express price


def test_instance_hour_soft_fail_returns_none():
    fake = MagicMock()
    fake.get_products.side_effect = RuntimeError("pricing down")
    with patch.object(rp, "_client", return_value=fake):
        rp._CACHE.clear()
        assert rp.price_rds_instance_hour("ap-northeast-2", "mysql", "db.t3.small") is None


def test_storage_month_gp3_plus_iops():
    fake = MagicMock()

    def products(**kw):
        fields = {f["Field"]: f["Value"] for f in kw["Filters"]}
        vt = fields.get("volumeType", "")
        if "IOPS" in fields.get("productFamily", "") or "PIOPS" in vt:
            return {"PriceList": [_product("APN2-RDS:PIOPS", 0.08, "Any")]}
        return {"PriceList": [_product("APN2-RDS:GP3-Storage", 0.114, "Any")]}

    fake.get_products.side_effect = products
    with patch.object(rp, "_client", return_value=fake):
        rp._CACHE.clear()
        out = rp.price_rds_storage_month("ap-northeast-2", "gp3", 100, provisioned_iops=None)
        assert out["storage_usd"] == 100 * 0.114
        assert out["iops_usd"] in (0.0, None)  # gp3 baseline 3000 IOPS free → 0 or null
    # gp3 MUST query its own volumeType, never gp2's "General Purpose" — else it
    # silently returns the gp2 rate for a gp3 request (fabricated wrong price).
    storage_filters = {f["Field"]: f["Value"] for f in fake.get_products.call_args_list[0].kwargs["Filters"]}
    assert storage_filters.get("volumeType") == "General Purpose-GP3"
