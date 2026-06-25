"""ElastiCache CloudWatch collector → metric_snapshots + cluster_meta upsert."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[3] / "data-pipeline/etl_collector/collectors/elasticache_cw_collector.py"
_spec = importlib.util.spec_from_file_location("ec_collector", _C)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _cw_with(value=42.0):
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {
        "Datapoints": [{"Timestamp": datetime(2026, 6, 24, 0, 0, 0), "Average": value, "Sum": value}]
    }
    return cw


def _ec_replication_group(name="my-redis", engine="redis"):
    """Return a mock ec client that answers describe_replication_groups with one group."""
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {
        "ReplicationGroups": [{
            "ReplicationGroupId": name,
            "Status": "available",
            "ClusterEnabled": True,
            "CacheNodeType": "cache.r6g.large",
            "AuthTokenEnabled": False,
            "TransitEncryptionEnabled": True,
            "NodeGroups": [{"NodeGroupId": "0001"}, {"NodeGroupId": "0002"}],
            "MemberClusters": [f"{name}-0001-001", f"{name}-0001-002",
                               f"{name}-0002-001", f"{name}-0002-002"],
            "EngineVersion": "7.1.0",
        }]
    }
    return ec


def _ec_cache_cluster(name="my-mc", engine="memcached"):
    """Return a mock ec client where describe_replication_groups raises,
    forcing the cache_cluster fallback path."""
    ec = MagicMock()
    ec.describe_replication_groups.side_effect = Exception("not a replication group")
    ec.describe_cache_clusters.return_value = {
        "CacheClusters": [{
            "CacheClusterId": name,
            "Engine": engine,
            "EngineVersion": "1.6.22",
            "CacheNodeType": "cache.m6g.large",
            "NumCacheNodes": 3,
            "CacheClusterStatus": "available",
            "AuthTokenEnabled": False,
            "TransitEncryptionEnabled": False,
        }]
    }
    return ec


# ── cluster_meta upsert tests ─────────────────────────────────────────────────

def test_cluster_meta_upserted_for_replication_group():
    """When describe_replication_groups returns a group, cache_execute is called
    with an INSERT INTO cluster_meta containing resource_details JSON."""
    cw = _cw_with()
    ec = _ec_replication_group("my-redis")
    meta_calls = []
    metric_rows = []

    def cache_execute(sql, params=None):
        if "cluster_meta" in sql:
            meta_calls.append((sql, params))
        elif params and "metric_type" in params:
            metric_rows.append(params["metric_type"])

    res = mod.collect_elasticache_metrics(
        cw, ec, cache_execute, "my-redis", "my-redis", "redis",
        "ap-northeast-2", "111122223333",
    )

    # One cluster_meta upsert must have happened
    assert len(meta_calls) == 1, f"expected 1 cluster_meta call, got {len(meta_calls)}"
    sql, params = meta_calls[0]
    assert "INSERT INTO cluster_meta" in sql
    assert "resource_details" in sql
    assert params["cid"] == "my-redis"
    assert params["account_id"] == "111122223333"
    assert params["region"] == "ap-northeast-2"

    # resource_details must be valid JSON with canonical fields
    details = json.loads(params["details"])
    assert details["engine"] == "redis"
    assert details["num_node_groups"] == 2
    assert details["replicas_per_node_group"] == 1  # (4 members / 2 groups) - 1
    assert details["cluster_mode"] is True
    assert details["tls_enabled"] is True

    # Metrics still collected
    assert res["metrics_inserted"] > 0
    assert "memory_usage_pct" in metric_rows


def test_cluster_meta_upserted_for_cache_cluster():
    """Cache-cluster path (Memcached / standalone) also upserts cluster_meta."""
    cw = _cw_with()
    ec = _ec_cache_cluster("my-mc", "memcached")
    meta_calls = []

    def cache_execute(sql, params=None):
        if "cluster_meta" in sql:
            meta_calls.append((sql, params))

    mod.collect_elasticache_metrics(
        cw, ec, cache_execute, "my-mc", "my-mc", "memcached",
        "ap-northeast-2", "111122223333",
    )

    assert len(meta_calls) == 1
    details = json.loads(meta_calls[0][1]["details"])
    assert details["engine"] == "memcached"
    assert details["num_cache_nodes"] == 3
    assert details["cluster_mode"] is False


def test_describe_failure_does_not_raise_and_metrics_still_collect():
    """A describe failure on ec must NOT raise and must NOT stop metric collection."""
    cw = _cw_with()
    ec = MagicMock()
    ec.describe_replication_groups.side_effect = Exception("network error")
    ec.describe_cache_clusters.side_effect = Exception("network error")

    metric_rows = []
    def cache_execute(sql, params=None):
        if params and "metric_type" in params:
            metric_rows.append(params["metric_type"])

    # Must not raise
    res = mod.collect_elasticache_metrics(
        cw, ec, cache_execute, "r", "r", "redis", "ap-northeast-2", "1",
    )

    # Errors captured but metrics still inserted
    assert any("describe_elasticache" in e for e in res["errors"])
    assert res["metrics_inserted"] > 0
    assert "memory_usage_pct" in metric_rows


# ── existing metric-collection tests ─────────────────────────────────────────

def test_redis_metrics_inserted_with_types():
    cw = _cw_with()
    ec = _ec_replication_group()
    rows = []
    def cache_execute(sql, params=None):
        if params and "metric_type" in params:
            rows.append(params["metric_type"])
    res = mod.collect_elasticache_metrics(cw, ec, cache_execute, "my-redis", "my-redis", "redis", "ap-northeast-2", "111122223333")
    assert res["metrics_inserted"] > 0
    # representative Redis metric types present
    assert "memory_usage_pct" in rows
    assert "evictions" in rows
    assert "curr_connections" in rows


def test_memcached_branch_subset():
    cw = _cw_with()
    ec = _ec_cache_cluster()
    rows = []
    def cache_execute(sql, params=None):
        if params and "metric_type" in params:
            rows.append(params["metric_type"])
    mod.collect_elasticache_metrics(cw, ec, cache_execute, "mc", "mc", "memcached", "ap-northeast-2", "111122223333")
    # memcached has no replication lag / DatabaseMemoryUsagePercentage
    assert "replication_lag" not in rows
    assert "get_hits" in rows or "curr_items" in rows


def test_empty_series_tolerated():
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": []}
    ec = _ec_replication_group()
    res = mod.collect_elasticache_metrics(cw, ec, lambda *a, **k: None, "r", "r", "redis", "ap-northeast-2", "1")
    assert res["metrics_inserted"] == 0  # no rows, no raise


# ── CloudWatch dimension = member NODE id, not replication-group id ───────────

def _cw_dim_values(cw):
    vals = set()
    for call in cw.get_metric_statistics.call_args_list:
        for d in call.kwargs["Dimensions"]:
            if d["Name"] == "CacheClusterId":
                vals.add(d["Value"])
    return vals


def test_cw_dimension_uses_member_node_ids_not_rg_id():
    """REGRESSION: AWS/ElastiCache emits metrics per NODE (CacheClusterId=<rg>-001),
    never under the replication-group id. The collector must query the member
    node ids — querying the RG id (resource_name) returns zero datapoints, so a
    replication-group cluster would silently collect nothing."""
    cw = _cw_with()
    ec = _ec_replication_group("my-redis")  # MemberClusters = my-redis-000{1,2}-00{1,2}
    res = mod.collect_elasticache_metrics(
        cw, ec, lambda *a, **k: None, "my-redis", "my-redis", "redis", "ap-northeast-2", "1")

    dims = _cw_dim_values(cw)
    assert dims, "no CacheClusterId dimension queried"
    assert "my-redis" not in dims, "queried the RG id (the bug) instead of node ids"
    assert dims <= {"my-redis-0001-001", "my-redis-0001-002",
                    "my-redis-0002-001", "my-redis-0002-002"}
    assert res["nodes"] == ["my-redis-0001-001", "my-redis-0001-002",
                            "my-redis-0002-001", "my-redis-0002-002"]


def test_cw_dimension_falls_back_to_resource_name_for_standalone():
    """Standalone / Memcached cache cluster: describe_replication_groups misses,
    so the node id IS the resource name and that is the CacheClusterId dimension."""
    cw = _cw_with()
    ec = _ec_cache_cluster("my-mc", "memcached")
    res = mod.collect_elasticache_metrics(
        cw, ec, lambda *a, **k: None, "my-mc", "my-mc", "memcached", "ap-northeast-2", "1")
    assert _cw_dim_values(cw) == {"my-mc"}
    assert res["nodes"] == ["my-mc"]


def test_sum_metrics_aggregate_across_member_nodes():
    """Sum-statistic metrics (e.g. NetworkBytesIn) total across member nodes at a
    timestamp; one aggregated row is written per timestamp, not one per node."""
    cw = _cw_with(value=10.0)  # every node/metric returns 10.0 at one timestamp
    ec = _ec_replication_group("my-redis")  # 4 member nodes
    rows = []

    def cache_execute(sql, params=None):
        if params and params.get("metric_type") == "net_in":  # NetworkBytesIn → Sum
            rows.append(params["value"])

    mod.collect_elasticache_metrics(
        cw, ec, cache_execute, "my-redis", "my-redis", "redis", "ap-northeast-2", "1")
    # 4 nodes × 10.0 summed = 40.0, written once (single timestamp)
    assert rows == [40.0]
