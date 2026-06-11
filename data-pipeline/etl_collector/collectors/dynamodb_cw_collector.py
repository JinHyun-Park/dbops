# data-pipeline/etl_collector/collectors/dynamodb_cw_collector.py
"""DynamoDB CloudWatch + describe_table meta → cache. Namespace AWS/DynamoDB.

Capacity-mode aware (Provisioned* only for provisioned tables). Throughput uses
Sum; latency requires an Operation dimension. Meta (billing mode, item count,
size, GSIs) goes to cluster_meta.resource_details (schema v16)."""
import json
from datetime import datetime, timedelta

_TABLE_METRICS_SUM = [
    ("ConsumedReadCapacityUnits", "consumed_rcu"),
    ("ConsumedWriteCapacityUnits", "consumed_wcu"),
    ("ReadThrottleEvents", "read_throttle_events"),
    ("WriteThrottleEvents", "write_throttle_events"),
    ("ThrottledRequests", "throttled_requests"),
    ("ReturnedItemCount", "returned_item_count"),
]
_PROVISIONED_METRICS_AVG = [
    ("ProvisionedReadCapacityUnits", "provisioned_rcu"),
    ("ProvisionedWriteCapacityUnits", "provisioned_wcu"),
]
_LATENCY_OPS = ["GetItem", "Query", "PutItem", "Scan"]


def _insert(cache_execute, cluster_id, ts, metric_type, value, dims="{}"):
    cache_execute(
        "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
        "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, :dims::jsonb) "
        "ON CONFLICT DO NOTHING",
        {"cluster_id": cluster_id, "ts": ts, "metric_type": metric_type,
         "value": float(value), "dims": dims})


def collect_dynamodb_metrics(cw, dynamo, cache_execute, cluster_id, table_name):
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted = 0
    errors = []

    billing_mode = "PROVISIONED"
    try:
        t = dynamo.describe_table(TableName=table_name)["Table"]
        billing_mode = (t.get("BillingModeSummary") or {}).get("BillingMode", "PROVISIONED")
        details = {
            "billing_mode": billing_mode,
            "item_count": t.get("ItemCount", 0),
            "table_size_bytes": t.get("TableSizeBytes", 0),
            "table_status": t.get("TableStatus", ""),
            "gsi": [g.get("IndexName") for g in t.get("GlobalSecondaryIndexes", [])],
        }
        cache_execute(
            "INSERT INTO cluster_meta (cluster_id, engine, resource_details, updated_at) "
            "VALUES (:cid, 'dynamodb', :details::jsonb, NOW()) "
            "ON CONFLICT (cluster_id) DO UPDATE SET resource_details = EXCLUDED.resource_details, "
            "engine = 'dynamodb', updated_at = NOW()",
            {"cid": cluster_id, "details": json.dumps(details)})
    except Exception as e:
        errors.append(f"describe_table: {e}")

    def pull(metric, stat, dims):
        try:
            return cw.get_metric_statistics(
                Namespace="AWS/DynamoDB", MetricName=metric, Dimensions=dims,
                StartTime=start, EndTime=end, Period=60, Statistics=[stat]
            ).get("Datapoints", [])
        except Exception as e:
            errors.append(f"{metric}: {e}")
            return []

    table_dim = [{"Name": "TableName", "Value": table_name}]

    for metric, mtype in _TABLE_METRICS_SUM:
        for dp in pull(metric, "Sum", table_dim):
            if dp.get("Sum") is None:
                continue
            _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, dp["Sum"])
            inserted += 1

    if billing_mode == "PROVISIONED":
        for metric, mtype in _PROVISIONED_METRICS_AVG:
            for dp in pull(metric, "Average", table_dim):
                if dp.get("Average") is None:
                    continue
                _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(), mtype, dp["Average"])
                inserted += 1

    for op in _LATENCY_OPS:
        dims = table_dim + [{"Name": "Operation", "Value": op}]
        for dp in pull("SuccessfulRequestLatency", "Average", dims):
            if dp.get("Average") is None:
                continue
            _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(),
                    f"latency_ms_{op.lower()}", dp["Average"],
                    json.dumps({"operation": op}))
            inserted += 1

    return {"cluster_id": cluster_id, "billing_mode": billing_mode,
            "metrics_inserted": inserted, "errors": errors}
