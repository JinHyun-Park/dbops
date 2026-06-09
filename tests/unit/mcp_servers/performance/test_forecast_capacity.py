from unittest.mock import MagicMock

from mcp_servers.performance.tools.forecast_capacity import forecast_capacity_impl
from mcp_servers.shared.models import QueryResult


def _cache(slope, current, r2=0.9, n=200, max_connections=None, instance_class=None):
    """Two execute() calls: metric trend, then cluster_meta. side_effect routes
    each by which columns the caller asked for."""
    metric_qr = QueryResult(
        columns=["slope_per_day", "r2", "n", "current_value"],
        rows=[{"slope_per_day": slope, "r2": r2, "n": n, "current_value": current}],
        row_count=1,
    )
    meta_qr = QueryResult(
        columns=["max_connections", "instance_class"],
        rows=[{"max_connections": max_connections, "instance_class": instance_class}],
        row_count=1,
    )
    cache = MagicMock()

    def _exec(sql, params=None):
        return meta_qr if "cluster_meta" in sql else metric_qr

    cache.execute.side_effect = _exec
    return cache


def test_storage_uses_aurora_volume_ceiling():
    cache = _cache(slope=2.5, current=180.0)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage_gb")
    assert result["current_value"] == 180.0
    assert result["limit"] == 131072  # Aurora 128 TiB, not the old 128000
    assert "days_until_limit" in result
    assert "128 TiB" in result["limit_basis"]


def test_connections_limit_from_cluster_meta():
    cache = _cache(slope=1.0, current=100.0, max_connections=2000)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["limit"] == 2000  # cluster's real max_connections, not 5000
    assert "max_connections" in result["limit_basis"]


def test_aas_limit_from_instance_vcpu():
    cache = _cache(slope=0.1, current=2.0, instance_class="db.r6g.4xlarge")
    result = forecast_capacity_impl(cache, cluster_id="c", metric="aas")
    assert result["limit"] == 16  # 4xlarge = 16 vCPU, not 64
    assert "vCPU=16" in result["limit_basis"]


def test_serverless_aas_falls_back_low_confidence():
    cache = _cache(slope=0.1, current=2.0, r2=0.9, n=200, instance_class="db.serverless")
    result = forecast_capacity_impl(cache, cluster_id="c", metric="aas")
    assert result["limit"] == 64  # fallback
    assert result["confidence"] == "low"  # ungrounded limit -> low


def test_confidence_high_with_good_fit_and_grounded_limit():
    cache = _cache(slope=2.5, current=180.0, r2=0.85, n=300)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage_gb")
    assert result["confidence"] == "high"
    assert result["days_until_limit_range"] is not None
    lo, hi = result["days_until_limit_range"]
    assert lo <= result["days_until_limit"]


def test_low_fit_is_low_confidence():
    cache = _cache(slope=2.5, current=180.0, r2=0.1, n=300)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage_gb")
    assert result["confidence"] == "low"
