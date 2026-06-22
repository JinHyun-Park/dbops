from datetime import datetime, timedelta

CW_METRICS = [
    {"name": "VolumeBytesUsed", "metric_type": "storage_bytes", "stat": "Average"},
    {"name": "AuroraReplicaLag", "metric_type": "replica_lag_ms", "stat": "Average"},
    {"name": "DatabaseConnections", "metric_type": "db_connections", "stat": "Average"},
    {"name": "FreeableMemory", "metric_type": "freeable_memory", "stat": "Average"},
    {"name": "FreeLocalStorage", "metric_type": "free_local_storage", "stat": "Average"},
    {"name": "Deadlocks", "metric_type": "deadlocks", "stat": "Sum"},
    {"name": "BufferCacheHitRatio", "metric_type": "buffer_cache_hit", "stat": "Average"},
    {"name": "EngineUptime", "metric_type": "uptime_sec", "stat": "Average"},
    # Serverless v2 현재 용량(ACU). 프로비저닝 클러스터는 이 메트릭을 내보내지
    # 않아 Datapoints가 비고, 아래 루프가 그냥 건너뛴다(무해). capacity_forecast의
    # ACU 고갈 예측이 이 값(metric_type=serverless_acu)을 일별 peak로 회귀한다.
    {"name": "ServerlessDatabaseCapacity", "metric_type": "serverless_acu", "stat": "Average"},
]


# Instance-dimensioned (DBInstanceIdentifier) metrics for the Compare "instance"
# mode. CPUUtilization is here (not in cluster CW_METRICS) because it's only
# meaningful per instance. AuroraReplicaLag is ~0 on the writer, real on readers.
CW_INSTANCE_METRICS = [
    {"name": "CPUUtilization", "metric_type": "cpu", "stat": "Average"},
    {"name": "AuroraReplicaLag", "metric_type": "replica_lag_ms", "stat": "Average"},
    {"name": "DatabaseConnections", "metric_type": "db_connections", "stat": "Average"},
    {"name": "FreeableMemory", "metric_type": "freeable_memory", "stat": "Average"},
    {"name": "FreeLocalStorage", "metric_type": "free_local_storage", "stat": "Average"},
    {"name": "ReadIOPS", "metric_type": "read_iops", "stat": "Average"},
    {"name": "WriteIOPS", "metric_type": "write_iops", "stat": "Average"},
    {"name": "ReadLatency", "metric_type": "read_latency", "stat": "Average"},
    {"name": "WriteLatency", "metric_type": "write_latency", "stat": "Average"},
    {"name": "NetworkReceiveThroughput", "metric_type": "net_rx", "stat": "Average"},
    {"name": "NetworkTransmitThroughput", "metric_type": "net_tx", "stat": "Average"},
    {"name": "BufferCacheHitRatio", "metric_type": "buffer_cache_hit", "stat": "Average"},
]


def collect_cw_instance_metrics(cw_client, cache_execute, cluster_id, instances):
    """Per-instance CloudWatch metrics tagged dimensions={instance,role}, stored
    alongside the cluster-level rows (which keep dimensions={}). Read by the
    Compare instance mode via the dimensions filter; invisible to cluster-level
    queries (which exclude rows where dimensions has an 'instance' key)."""
    import json
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=10)
    inserted = 0
    errors = []
    for inst in instances or []:
        iid = inst.get("id")
        if not iid:
            continue
        dims_json = json.dumps({"instance": iid, "role": inst.get("role", "")})
        for m in CW_INSTANCE_METRICS:
            try:
                resp = cw_client.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName=m["name"],
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": iid}],
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=60,
                    Statistics=[m["stat"]],
                )
            except Exception as e:
                errors.append(f"{iid}/{m['metric_type']}: {e}")
                continue
            for dp in resp.get("Datapoints", []):
                value = dp.get(m["stat"])
                if value is None:
                    continue
                cache_execute(
                    "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
                    "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, :dimensions::jsonb) "
                    "ON CONFLICT DO NOTHING",
                    {
                        "cluster_id": cluster_id,
                        "ts": dp["Timestamp"].isoformat(),
                        "metric_type": m["metric_type"],
                        "value": float(value),
                        "dimensions": dims_json,
                    },
                )
                inserted += 1
    return {"cluster_id": cluster_id, "metrics_inserted": inserted, "errors": errors}


def collect_cw_metrics(cw_client, cache_execute, cluster_id):
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=10)

    inserted = 0
    errors = []

    for m in CW_METRICS:
        try:
            resp = cw_client.get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName=m["name"],
                Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=60,
                Statistics=[m["stat"]],
            )
        except Exception as e:
            errors.append(f"{m['metric_type']}: {e}")
            continue

        for dp in resp.get("Datapoints", []):
            value = dp.get(m["stat"])
            if value is None:
                continue
            cache_execute(
                "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
                "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, '{}'::jsonb) "
                "ON CONFLICT DO NOTHING",
                {
                    "cluster_id": cluster_id,
                    "ts": dp["Timestamp"].isoformat(),
                    "metric_type": m["metric_type"],
                    "value": float(value),
                },
            )
            inserted += 1

    return {"cluster_id": cluster_id, "metrics_inserted": inserted, "errors": errors}
