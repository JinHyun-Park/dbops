# data-pipeline/etl_collector/collectors/elasticache_cw_collector.py
"""ElastiCache CloudWatch → cache. Namespace AWS/ElastiCache, dimension
CacheClusterId. Redis/Valkey use the full metric set; Memcached uses a subset
(no replication/persistence). Cluster-level rows only (dimensions='{}')."""
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


def collect_elasticache_metrics(cw, ec, cache_execute, cluster_id, resource_name, engine, region, account_id):
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted = 0
    errors = []
    eng = (engine or "redis").lower()
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
