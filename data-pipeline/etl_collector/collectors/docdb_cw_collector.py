"""DocumentDB CloudWatch + meta → cache. Namespace AWS/DocDB.

Cluster-scoped metrics use DBClusterIdentifier; instance-scoped use the writer's
DBInstanceIdentifier (DocDB publishes CPU/connections/cache-hit per instance)."""
import json
from datetime import datetime, timedelta

_CLUSTER_METRICS = [
    ("DBClusterReplicaLagMaximum", "replica_lag_ms", "Average"),
    ("VolumeBytesUsed", "storage_bytes", "Average"),
]
_INSTANCE_METRICS = [
    ("CPUUtilization", "cpu_utilization", "Average"),
    ("DatabaseConnections", "db_connections", "Average"),
    ("DatabaseCursors", "cursors", "Average"),
    ("DatabaseCursorsTimedOut", "cursors_timed_out", "Sum"),
    ("BufferCacheHitRatio", "buffer_cache_hit", "Average"),
    ("FreeableMemory", "freeable_memory", "Average"),
    ("ReadLatency", "read_latency_ms", "Average"),
    ("WriteLatency", "write_latency_ms", "Average"),
    ("DiskQueueDepth", "disk_queue_depth", "Average"),
    ("OpcountersQuery", "opcounter_query", "Average"),
    ("OpcountersInsert", "opcounter_insert", "Average"),
    ("OpcountersUpdate", "opcounter_update", "Average"),
    ("OpcountersDelete", "opcounter_delete", "Average"),
]


def _insert(cache_execute, cluster_id, ts, metric_type, value):
    cache_execute(
        "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
        "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, '{}'::jsonb) "
        "ON CONFLICT DO NOTHING",
        {"cluster_id": cluster_id, "ts": ts, "metric_type": metric_type, "value": float(value)})


def collect_docdb_metrics(cw, docdb, cache_execute, cluster_id, region, account_id):
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted = 0
    errors = []
    writer = None
    try:
        c = docdb.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
        members = c.get("DBClusterMembers", [])
        writer = next((m["DBInstanceIdentifier"] for m in members if m.get("IsClusterWriter")),
                      members[0]["DBInstanceIdentifier"] if members else None)
        details = {"instances": [m.get("DBInstanceIdentifier") for m in members],
                   "instance_count": len(members)}
        cache_execute(
            "INSERT INTO cluster_meta (cluster_id, engine, engine_version, status, resource_details, updated_at) "
            "VALUES (:cid, 'docdb', :ver, :status, :details::jsonb, NOW()) "
            "ON CONFLICT (cluster_id) DO UPDATE SET engine='docdb', engine_version=EXCLUDED.engine_version, "
            "status=EXCLUDED.status, resource_details=EXCLUDED.resource_details, updated_at=NOW()",
            {"cid": cluster_id, "ver": c.get("EngineVersion", ""), "status": c.get("Status", ""),
             "details": json.dumps(details)})
    except Exception as e:
        errors.append(f"describe_db_clusters: {e}")

    def pull(metric, stat, dims):
        try:
            return cw.get_metric_statistics(
                Namespace="AWS/DocDB", MetricName=metric, Dimensions=dims,
                StartTime=start, EndTime=end, Period=60, Statistics=[stat]).get("Datapoints", [])
        except Exception as e:
            errors.append(f"{metric}: {e}")
            return []

    for metric, mtype, stat in _CLUSTER_METRICS:
        for dp in pull(metric, stat, [{"Name": "DBClusterIdentifier", "Value": cluster_id}]):
            if dp.get(stat) is None:
                continue
            _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, dp[stat])
            inserted += 1

    if writer:
        for metric, mtype, stat in _INSTANCE_METRICS:
            for dp in pull(metric, stat, [{"Name": "DBInstanceIdentifier", "Value": writer}]):
                if dp.get(stat) is None:
                    continue
                _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, dp[stat])
                inserted += 1

    return {"cluster_id": cluster_id, "writer": writer, "metrics_inserted": inserted, "errors": errors}
