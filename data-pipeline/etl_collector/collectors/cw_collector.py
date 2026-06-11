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
