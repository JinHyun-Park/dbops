from unittest.mock import MagicMock, patch

from mcp_servers.simulation.tools import rds_rightsizing as rr


class _Cache:
    def __init__(self, meta_row, metric_rows):
        self._meta = meta_row
        self._metrics = metric_rows

    def execute(self, sql, params=None):
        if "cluster_meta" in sql:
            return [self._meta]
        return self._metrics


def _meta(engine="sqlserver-ex"):
    # resource_details keys are exactly what rds_instance_cw_collector writes.
    return {"engine": engine, "instance_class": "db.t3.small", "region": "ap-northeast-2",
            "resource_details": {"storage_type": "gp3", "allocated_storage_gb": 20,
                                 "multi_az": False, "license_model": "license-included"}}


def _metrics(cpu_p95=6.0, conn_peak=2, read_p95=0.0, write_p95=1.0, mem_min=800.0, samples=2016):
    # tool aggregates in SQL; the stub returns the already-aggregated single row
    return [{"cpu_p95": cpu_p95, "cpu_avg": cpu_p95 / 2, "conn_peak": conn_peak,
             "read_iops_p95": read_p95, "write_iops_p95": write_p95,
             "freeable_mem_min_mb": mem_min, "samples": samples}]


def test_underutilized_recommends_downsize_and_cheaper():
    cache = _Cache(_meta(), _metrics(cpu_p95=6.0))
    with patch.object(rr, "price_rds_instance_hour", side_effect=[0.052, 0.026]), \
         patch.object(rr, "price_rds_storage_month", return_value={"storage_usd": 2.28, "iops_usd": 0.0}):
        out = rr.simulate_rds_instance_rightsizing_impl(cache, cluster_id="dbops-demo-mssql")
    assert out["status"] == "ok"
    assert out["recommendation"]["action"] == "downsize"
    assert out["recommendation"]["instance_class"] == "db.t3.micro"
    assert out["cost_impact"]["delta_monthly_usd"] < 0
    assert out["cost_impact"]["pricing_source"] == "aws_price_list"


def test_hot_recommends_upsize():
    cache = _Cache(_meta("mysql"), _metrics(cpu_p95=88.0, conn_peak=90))
    with patch.object(rr, "price_rds_instance_hour", side_effect=[0.045, 0.09]), \
         patch.object(rr, "price_rds_storage_month", return_value={"storage_usd": 2.28, "iops_usd": 0.0}):
        out = rr.simulate_rds_instance_rightsizing_impl(cache, cluster_id="dbops-demo-mysql")
    assert out["recommendation"]["action"] == "upsize"
    assert out["cost_impact"]["delta_monthly_usd"] > 0


def test_null_price_marks_fallback_never_fabricates():
    cache = _Cache(_meta(), _metrics(cpu_p95=6.0))
    with patch.object(rr, "price_rds_instance_hour", return_value=None), \
         patch.object(rr, "price_rds_storage_month", return_value={"storage_usd": None, "iops_usd": None}):
        out = rr.simulate_rds_instance_rightsizing_impl(cache, cluster_id="dbops-demo-mssql")
    assert out["cost_impact"]["pricing_source"] == "fallback_estimate"
    assert out["cost_impact"]["current_monthly_usd"] is None


def test_insufficient_data_when_no_metrics():
    cache = _Cache(_meta(), [{"cpu_p95": None, "samples": 0}])
    out = rr.simulate_rds_instance_rightsizing_impl(cache, cluster_id="dbops-demo-mssql")
    assert out["status"] == "insufficient_data"
