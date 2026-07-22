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
    # The filter must have carried the Express databaseEngine label.
    sent = fake.get_products.call_args.kwargs["Filters"]
    assert any(f["Field"] == "databaseEngine" and f["Value"] == rp.RDS_ENGINE_LABEL["sqlserver-ex"] for f in sent)


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
