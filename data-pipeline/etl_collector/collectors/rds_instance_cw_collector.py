"""RDS instance (non-Aurora MySQL / SQL Server) CloudWatch + meta -> cache.

Namespace AWS/RDS with the DBInstanceIdentifier dimension — standalone DB
instances never expose DBClusterIdentifier. Rows land with dimensions='{}'
(cluster-scoped) because the instance IS the monitored resource, so triage /
alerts / capacity forecast read them unmodified."""
import json
from datetime import datetime, timedelta

_METRICS = [
    ("CPUUtilization", "cpu", "Average"),
    ("DatabaseConnections", "db_connections", "Average"),
    ("FreeableMemory", "freeable_memory", "Average"),
    ("FreeStorageSpace", "free_storage_bytes", "Average"),
    ("ReadIOPS", "read_iops", "Average"),
    ("WriteIOPS", "write_iops", "Average"),
    ("ReadLatency", "read_latency", "Average"),
    ("WriteLatency", "write_latency", "Average"),
    ("NetworkReceiveThroughput", "net_rx", "Average"),
    ("NetworkTransmitThroughput", "net_tx", "Average"),
    ("SwapUsage", "swap_usage", "Average"),
]


def collect_rds_instance_metrics(cw, rds_client, cache_execute, cluster_id, region, account_id):
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted, errors = 0, []
    resource_id, pi_enabled = None, False

    try:
        inst = rds_client.describe_db_instances(
            DBInstanceIdentifier=cluster_id)["DBInstances"][0]
        resource_id = inst.get("DbiResourceId")
        pi_enabled = bool(inst.get("PerformanceInsightsEnabled"))
        endpoint = inst.get("Endpoint") or {}
        details = {
            "instance_class": inst.get("DBInstanceClass"),
            "multi_az": bool(inst.get("MultiAZ")),
            "storage_type": inst.get("StorageType"),
            "allocated_storage_gb": inst.get("AllocatedStorage"),
            "license_model": inst.get("LicenseModel"),
            "publicly_accessible": bool(inst.get("PubliclyAccessible")),
            "pi_enabled": pi_enabled,
            "endpoint": endpoint.get("Address"),
            "port": endpoint.get("Port"),
        }
        cache_execute(
            "INSERT INTO cluster_meta (cluster_id, account_id, region, engine, engine_version, instance_class, status, resource_details, updated_at) "
            "VALUES (:cid, :account_id, :region, :engine, :ver, :cls, :status, :details::jsonb, NOW()) "
            "ON CONFLICT (cluster_id) DO UPDATE SET engine=EXCLUDED.engine, "
            "engine_version=EXCLUDED.engine_version, instance_class=EXCLUDED.instance_class, "
            "status=EXCLUDED.status, resource_details=EXCLUDED.resource_details, updated_at=NOW()",
            {"cid": cluster_id, "account_id": account_id, "region": region,
             "engine": inst.get("Engine", ""), "ver": inst.get("EngineVersion", ""),
             "cls": inst.get("DBInstanceClass", ""),
             "status": inst.get("DBInstanceStatus", ""),
             "details": json.dumps(details)})
    except Exception as e:
        errors.append(f"describe_db_instances: {e}")

    for metric, mtype, stat in _METRICS:
        try:
            dps = cw.get_metric_statistics(
                Namespace="AWS/RDS", MetricName=metric,
                Dimensions=[{"Name": "DBInstanceIdentifier", "Value": cluster_id}],
                StartTime=start, EndTime=end, Period=60, Statistics=[stat],
            ).get("Datapoints", [])
        except Exception as e:
            errors.append(f"{mtype}: {e}")
            continue
        for dp in dps:
            value = dp.get(stat)
            if value is None:
                continue
            cache_execute(
                "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
                "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, '{}'::jsonb) "
                "ON CONFLICT DO NOTHING",
                {"cluster_id": cluster_id, "ts": dp["Timestamp"].isoformat(),
                 "metric_type": mtype, "value": float(value)})
            inserted += 1

    return {"cluster_id": cluster_id, "metrics_inserted": inserted,
            "errors": errors, "resource_id": resource_id, "pi_enabled": pi_enabled}
