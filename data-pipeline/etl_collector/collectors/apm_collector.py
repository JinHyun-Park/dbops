# data-pipeline/etl_collector/collectors/apm_collector.py
"""APM collector for EC2 Java/Spring Boot targets.

Pulls host + APM metrics (CloudWatch GetMetricStatistics) and per-level log
COUNTS (Logs Insights `stats count() by level`) into the Aurora PG cache. Raw
log lines are never stored — those are fetched on demand by api/apm at search
time. Read-only against CloudWatch.
"""
import json
import time
from datetime import datetime, timedelta

# (metric_name, namespace, dimension_name, metric_type, statistic)
_METRICS = [
    ("CPUUtilization", "AWS/EC2", "InstanceId", "cpu", "Average"),
    ("mem_used_percent", "CWAgent", "InstanceId", "mem", "Average"),
    ("disk_used_percent", "CWAgent", "InstanceId", "disk", "Average"),
    ("Latency", "ApplicationSignals", "Service", "latency_p99", "Average"),
    ("Error", "ApplicationSignals", "Service", "error_rate", "Average"),
    ("Fault", "ApplicationSignals", "Service", "fault_rate", "Average"),
]

_LEVEL_QUERY = "stats count(*) as cnt by level | sort cnt desc | limit 20"


def _metric_dim(dim_name, target):
    if dim_name == "InstanceId":
        return target.get("instance_id", "")
    if dim_name == "Service":
        return target.get("service_name", "")
    return ""


def collect_apm(cw, logs_client, cache_execute, target):
    target_id = target["target_id"]
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted, log_buckets, errors = 0, 0, []

    # 1) host + APM metrics
    for metric, namespace, dim_name, mtype, stat in _METRICS:
        dim_value = _metric_dim(dim_name, target)
        if not dim_value:
            continue
        try:
            dps = cw.get_metric_statistics(
                Namespace=namespace, MetricName=metric,
                Dimensions=[{"Name": dim_name, "Value": dim_value}],
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
                "INSERT INTO apm_metric_snapshots (target_id, ts, metric_type, value, dimensions) "
                "VALUES (:target_id, :ts::timestamptz, :metric_type, :value, '{}'::jsonb) "
                "ON CONFLICT DO NOTHING",
                {"target_id": target_id, "ts": dp["Timestamp"].isoformat(),
                 "metric_type": mtype, "value": float(value)})
            inserted += 1

    # 2) per-level log COUNTS (no raw lines)
    bucket_ts = end.replace(second=0, microsecond=0).isoformat()
    for log_group in target.get("log_groups") or []:
        try:
            qid = logs_client.start_query(
                logGroupName=log_group,
                startTime=int((end - timedelta(minutes=5)).timestamp() * 1000),
                endTime=int(end.timestamp() * 1000),
                queryString=f"fields @message | {_LEVEL_QUERY}",
            )["queryId"]
            rows = None
            for _ in range(25):
                r = logs_client.get_query_results(queryId=qid)
                if r.get("status") == "Complete":
                    rows = r.get("results", []) or []
                    break
                if r.get("status") in ("Failed", "Cancelled"):
                    errors.append(f"{log_group}: query {r.get('status')}")
                    break
                time.sleep(1)
            for row in rows or []:
                fields = {f["field"]: f["value"] for f in row}
                level = (fields.get("level") or "").upper()[:16]
                cnt = int(float(fields.get("cnt", "0")))
                if not level:
                    continue
                cache_execute(
                    "INSERT INTO apm_log_level_counts (target_id, ts, log_group, level, count) "
                    "VALUES (:target_id, :ts::timestamptz, :log_group, :level, :count) "
                    "ON CONFLICT (target_id, ts, log_group, level) DO UPDATE SET count=EXCLUDED.count",
                    {"target_id": target_id, "ts": bucket_ts, "log_group": log_group,
                     "level": level, "count": cnt})
                log_buckets += 1
        except Exception as e:
            errors.append(f"{log_group}: {e}")

    # 3) meta mirror
    try:
        cache_execute(
            "INSERT INTO apm_target_meta (target_id, instance_id, region, service_name, log_groups, team, last_seen_at) "
            "VALUES (:target_id, :instance_id, :region, :service_name, :log_groups::jsonb, :team, NOW()) "
            "ON CONFLICT (target_id) DO UPDATE SET instance_id=EXCLUDED.instance_id, region=EXCLUDED.region, "
            "service_name=EXCLUDED.service_name, log_groups=EXCLUDED.log_groups, team=EXCLUDED.team, last_seen_at=NOW()",
            {"target_id": target_id, "instance_id": target.get("instance_id", ""),
             "region": target.get("region", ""), "service_name": target.get("service_name", ""),
             "log_groups": json.dumps(target.get("log_groups") or []), "team": target.get("team", "")})
    except Exception as e:
        errors.append(f"meta: {e}")

    return {"target_id": target_id, "metrics_inserted": inserted,
            "log_buckets_inserted": log_buckets, "errors": errors}
