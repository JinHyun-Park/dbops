# data-pipeline/etl_collector/collectors/elasticache_cw_collector.py
"""ElastiCache CloudWatch → cache. Namespace AWS/ElastiCache, dimension
CacheClusterId. Redis/Valkey use the full metric set; Memcached uses a subset
(no replication/persistence). Cluster-level rows only (dimensions='{}')."""
import json
from datetime import datetime, timedelta

# (metric_name, metric_type, statistic)
_REDIS_METRICS = [
    ("CPUUtilization", "cache_cpu", "Average"),
    ("EngineCPUUtilization", "engine_cpu", "Average"),
    ("DatabaseMemoryUsagePercentage", "memory_usage_pct", "Average"),
    ("BytesUsedForCache", "bytes_used", "Average"),
    ("CacheHits", "cache_hits", "Sum"),
    ("CacheMisses", "cache_misses", "Sum"),
    ("CurrConnections", "curr_connections", "Average"),
    ("NewConnections", "new_connections", "Sum"),
    ("Evictions", "evictions", "Sum"),
    ("Reclaimed", "reclaimed", "Sum"),
    ("ReplicationLag", "replication_lag", "Average"),
    ("SwapUsage", "swap_usage", "Average"),
    ("FreeableMemory", "freeable_memory", "Average"),
    ("CurrItems", "curr_items", "Average"),
    ("NetworkBytesIn", "net_in", "Sum"),
    ("NetworkBytesOut", "net_out", "Sum"),
]
_MEMCACHED_METRICS = [
    ("CPUUtilization", "cache_cpu", "Average"),
    ("FreeableMemory", "freeable_memory", "Average"),
    ("SwapUsage", "swap_usage", "Average"),
    ("CurrConnections", "curr_connections", "Average"),
    ("NewConnections", "new_connections", "Sum"),
    ("Evictions", "evictions", "Sum"),
    ("Reclaimed", "reclaimed", "Sum"),
    ("CurrItems", "curr_items", "Average"),
    ("BytesUsedForCacheItems", "bytes_used", "Average"),
    ("GetHits", "get_hits", "Sum"),
    ("GetMisses", "get_misses", "Sum"),
    ("NetworkBytesIn", "net_in", "Sum"),
    ("NetworkBytesOut", "net_out", "Sum"),
]


def _insert(cache_execute, cluster_id, ts, metric_type, value):
    cache_execute(
        "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
        "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, :dims::jsonb) "
        "ON CONFLICT DO NOTHING",
        {"cluster_id": cluster_id, "ts": ts, "metric_type": metric_type,
         "value": float(value), "dims": "{}"})


def _upsert_cluster_meta(ec, cache_execute, cluster_id, resource_name, engine, account_id, region, errors):
    """Describe the ElastiCache cluster and upsert cluster_meta.resource_details.

    Tries describe_replication_groups first (Redis/Valkey replication group),
    falls back to describe_cache_clusters (Memcached / standalone node).
    Any describe or upsert failure is appended to errors and silently swallowed
    so the caller's metric-collection loop is never interrupted.
    """
    try:
        details = None
        eng = (engine or "redis").lower()
        # -- Redis / Valkey replication group path --
        try:
            rg_resp = ec.describe_replication_groups(ReplicationGroupId=resource_name)
            rg_list = rg_resp.get("ReplicationGroups") or []
            if rg_list:
                g = rg_list[0]
                node_groups = g.get("NodeGroups") or []
                members = g.get("MemberClusters") or []
                details = {
                    "engine": eng,
                    "engine_version": g.get("EngineVersion", "") or "",
                    "node_type": g.get("CacheNodeType", ""),
                    "num_node_groups": len(node_groups),
                    "replicas_per_node_group": max(
                        0,
                        (len(members) // max(1, len(node_groups))) - 1,
                    ),
                    "num_cache_nodes": len(members),
                    "cluster_mode": bool(g.get("ClusterEnabled", False)),
                    "auth_enabled": bool(g.get("AuthTokenEnabled", False)),
                    "tls_enabled": bool(g.get("TransitEncryptionEnabled", False)),
                    "status": g.get("Status", ""),
                }
        except Exception:
            pass  # fall through to cache_cluster path

        # -- Memcached / standalone cache cluster path --
        if details is None:
            cc_resp = ec.describe_cache_clusters(
                CacheClusterId=resource_name, ShowCacheNodeInfo=True
            )
            cc_list = cc_resp.get("CacheClusters") or []
            if cc_list:
                c = cc_list[0]
                eng = (c.get("Engine") or eng).lower()
                details = {
                    "engine": eng,
                    "engine_version": c.get("EngineVersion", "") or "",
                    "node_type": c.get("CacheNodeType", ""),
                    "num_node_groups": 0,
                    "replicas_per_node_group": 0,
                    "num_cache_nodes": c.get("NumCacheNodes", 0),
                    "cluster_mode": False,
                    "auth_enabled": bool(c.get("AuthTokenEnabled", False)),
                    "tls_enabled": bool(c.get("TransitEncryptionEnabled", False)),
                    "status": c.get("CacheClusterStatus", ""),
                }

        if details is not None:
            cache_execute(
                "INSERT INTO cluster_meta "
                "(cluster_id, account_id, region, engine, resource_details, updated_at) "
                "VALUES (:cid, :account_id, :region, :engine, :details::jsonb, NOW()) "
                "ON CONFLICT (cluster_id) DO UPDATE SET "
                "resource_details = EXCLUDED.resource_details, "
                "engine = EXCLUDED.engine, "
                "updated_at = NOW()",
                {
                    "cid": cluster_id,
                    "account_id": account_id,
                    "region": region,
                    "engine": details["engine"],
                    "details": json.dumps(details),
                },
            )
    except Exception as e:
        errors.append(f"describe_elasticache: {e}")


def collect_elasticache_metrics(cw, ec, cache_execute, cluster_id, resource_name, engine, region, account_id):
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted = 0
    errors = []
    eng = (engine or "redis").lower()

    # Upsert cluster_meta.resource_details before metric collection.
    # A describe failure is captured in errors and never raises.
    _upsert_cluster_meta(ec, cache_execute, cluster_id, resource_name, engine, account_id, region, errors)

    metrics = _MEMCACHED_METRICS if eng == "memcached" else _REDIS_METRICS
    dims = [{"Name": "CacheClusterId", "Value": resource_name}]

    def pull(metric, stat):
        try:
            return cw.get_metric_statistics(
                Namespace="AWS/ElastiCache", MetricName=metric, Dimensions=dims,
                StartTime=start, EndTime=end, Period=60, Statistics=[stat]
            ).get("Datapoints", [])
        except Exception as e:
            errors.append(f"{metric}: {e}")
            return []

    for metric, mtype, stat in metrics:
        for dp in pull(metric, stat):
            v = dp.get(stat)
            if v is None:
                continue
            _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, v)
            inserted += 1

    return {"cluster_id": cluster_id, "engine": eng,
            "metrics_inserted": inserted, "errors": errors}
