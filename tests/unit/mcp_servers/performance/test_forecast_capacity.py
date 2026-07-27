from unittest.mock import MagicMock

from mcp_servers.performance.tools.forecast_capacity import forecast_capacity_impl
from mcp_servers.shared.models import QueryResult

_GIB = 1024 ** 3
_VOLUME_MAX_BYTES = 128 * 1024 ** 4


def _cache(slope, current, r2=0.9, n=200, max_connections=None, instance_class=None,
           engine="aurora-postgresql", allocated_storage_gb=None):
    """Two execute() calls: cluster_meta, then the metric trend. side_effect
    routes each by which table the caller asked for. `seen` records the
    metric_type the trend query was actually parameterized with."""
    metric_qr = QueryResult(
        columns=["slope_per_day", "r2", "n", "current_value"],
        rows=[{"slope_per_day": slope, "r2": r2, "n": n, "current_value": current}],
        row_count=1,
    )
    meta_qr = QueryResult(
        columns=["engine", "max_connections", "instance_class", "allocated_storage_gb"],
        rows=[{"engine": engine, "max_connections": max_connections,
               "instance_class": instance_class,
               "allocated_storage_gb": allocated_storage_gb}],
        row_count=1,
    )
    cache = MagicMock()
    cache.seen = {}

    def _exec(sql, params=None):
        if "cluster_meta" in sql:
            return meta_qr
        cache.seen["metric_type"] = (params or {}).get("metric")
        return metric_qr

    cache.execute.side_effect = _exec
    return cache


# ===== storage: the GROWING shape (Aurora / DocumentDB storage_bytes) =====


def test_aurora_storage_reads_storage_bytes_and_volume_ceiling():
    """The old default metric `storage_gb` is written by NO collector, so the
    default path returned zero samples. Aurora must read `storage_bytes` and
    compare it against the real 128 TiB volume ceiling (in bytes)."""
    cache = _cache(slope=2.0 * _GIB, current=100.0 * _GIB)
    result = forecast_capacity_impl(cache, cluster_id="c")  # default metric
    assert cache.seen["metric_type"] == "storage_bytes"
    assert result["metric"] == "storage"
    assert result["limit"] == _VOLUME_MAX_BYTES
    assert result["forecast"] == "growing"
    assert result["days_until_limit"] > 0
    assert "128 TiB" in result["limit_basis"]


def test_documentdb_storage_uses_the_same_growing_series():
    cache = _cache(slope=1.0 * _GIB, current=50.0 * _GIB, engine="docdb")
    result = forecast_capacity_impl(cache, cluster_id="docdb-1", metric="storage")
    assert result["engine_family"] == "documentdb"
    assert cache.seen["metric_type"] == "storage_bytes"
    assert result["days_until_limit"] > 0


def test_aurora_storage_shrinking_claims_no_date():
    """A DECREASING volume is moving away from the ceiling, so no ETA."""
    cache = _cache(slope=-1.0 * _GIB, current=100.0 * _GIB)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage")
    assert result["days_until_limit"] == -1
    assert result["days_until_limit_range"] is None
    assert result["confidence"] == "low"


# ===== storage: the SHRINKING shape (standalone RDS free_storage_bytes) =====


def test_rds_instance_storage_forecasts_exhaustion_from_free_space():
    """rds_instance collects FreeStorageSpace (free_storage_bytes), which SHRINKS
    toward 0. 20 GiB free losing 2 GiB/day → ~10 days to STORAGE_FULL."""
    cache = _cache(slope=-2.0 * _GIB, current=20.0 * _GIB,
                   engine="mysql", allocated_storage_gb="100")
    result = forecast_capacity_impl(cache, cluster_id="rds-mysql-1", metric="storage")
    assert result["engine_family"] == "rds_instance"
    assert cache.seen["metric_type"] == "free_storage_bytes"
    assert result["limit"] == 0.0
    assert result["forecast"] == "shrinking"
    assert result["days_until_limit"] == 10
    assert result["grounded"] is True
    # allocated_storage_gb from cluster_meta.resource_details gives usage context.
    assert result["allocated_gb"] == 100.0
    assert result["usage_pct"] == 80.0


def test_rds_instance_free_space_growing_claims_no_date():
    """Free space GROWING means the disk is emptying, not filling, so no ETA."""
    cache = _cache(slope=1.0 * _GIB, current=20.0 * _GIB, engine="sqlserver-se")
    result = forecast_capacity_impl(cache, cluster_id="rds-mssql-1", metric="storage")
    assert cache.seen["metric_type"] == "free_storage_bytes"
    assert result["days_until_limit"] == -1
    assert result["days_until_limit_range"] is None


def test_rds_instance_without_allocated_storage_still_forecasts_exhaustion():
    """The 0-byte floor is a hard fact, so a missing allocated_storage_gb only
    drops the usage_pct context, the exhaustion ETA stays valid."""
    cache = _cache(slope=-1.0 * _GIB, current=5.0 * _GIB, engine="mysql")
    result = forecast_capacity_impl(cache, cluster_id="rds-mysql-2", metric="storage")
    assert result["days_until_limit"] == 5
    assert "allocated_gb" not in result
    assert "usage_pct" not in result


def test_storage_unsupported_engine_refuses_instead_of_zero_forecast():
    """DynamoDB/ElastiCache have no storage series at all, so say so instead of
    reporting a flat 0-sample trend."""
    for engine in ("dynamodb", "redis"):
        cache = _cache(slope=0.0, current=0.0, engine=engine)
        result = forecast_capacity_impl(cache, cluster_id="x", metric="storage")
        assert result["status"] == "unsupported_metric"
        assert result["days_until_limit"] is None
        assert result["samples"] == 0
        assert cache.seen == {}  # trend query never ran


def test_unknown_metric_is_refused():
    cache = _cache(slope=1.0, current=1.0)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage_gb")
    assert result["status"] == "unsupported_metric"
    assert result["days_until_limit"] is None


# ===== connections / aas =====


def test_connections_limit_from_cluster_meta_and_canonical_series():
    cache = _cache(slope=1.0, current=100.0, max_connections=2000)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["limit"] == 2000  # cluster's real max_connections, not 5000
    assert "max_connections" in result["limit_basis"]
    # db_connections (CloudWatch) is collected for every engine; the PI-only
    # `connections` series is empty whenever Performance Insights is off.
    assert cache.seen["metric_type"] == "db_connections"


def test_aas_limit_from_instance_vcpu():
    cache = _cache(slope=0.1, current=2.0, instance_class="db.r6g.4xlarge")
    result = forecast_capacity_impl(cache, cluster_id="c", metric="aas")
    assert result["limit"] == 16  # 4xlarge = 16 vCPU, not 64
    assert "vCPU=16" in result["limit_basis"]
    assert cache.seen["metric_type"] == "aas"


def test_ungrounded_limit_claims_no_date():
    """Serverless (vCPU unknown) → the limit is an assumption, so no ETA is
    asserted: days_until_limit is None, not a number."""
    cache = _cache(slope=0.1, current=2.0, r2=0.9, n=200, instance_class="db.serverless")
    result = forecast_capacity_impl(cache, cluster_id="c", metric="aas")
    assert result["limit"] == 64  # fallback, flagged
    assert result["grounded"] is False
    assert result["days_until_limit"] is None
    assert result["days_until_limit_range"] is None
    assert result["confidence"] == "low"
    assert "단정하지 않습니다" in result["note"]


def test_ungrounded_connections_claims_no_date():
    cache = _cache(slope=5.0, current=100.0)  # no max_connections in cluster_meta
    result = forecast_capacity_impl(cache, cluster_id="c", metric="connections")
    assert result["grounded"] is False
    assert result["days_until_limit"] is None


# ===== confidence banding =====


def test_confidence_high_with_good_fit_and_grounded_limit():
    cache = _cache(slope=2.0 * _GIB, current=100.0 * _GIB, r2=0.85, n=300)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage")
    assert result["confidence"] == "high"
    assert result["days_until_limit_range"] is not None
    lo, hi = result["days_until_limit_range"]
    assert lo <= result["days_until_limit"]


def test_low_fit_is_low_confidence():
    cache = _cache(slope=2.0 * _GIB, current=100.0 * _GIB, r2=0.1, n=300)
    result = forecast_capacity_impl(cache, cluster_id="c", metric="storage")
    assert result["confidence"] == "low"
