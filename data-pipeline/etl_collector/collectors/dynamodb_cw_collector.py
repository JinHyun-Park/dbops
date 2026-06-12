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
_GSI_METRICS_SUM = [
    ("ConsumedReadCapacityUnits", "consumed_rcu"),
    ("ConsumedWriteCapacityUnits", "consumed_wcu"),
    ("ReadThrottleEvents", "read_throttle_events"),
    ("WriteThrottleEvents", "write_throttle_events"),
]


def _insert(cache_execute, cluster_id, ts, metric_type, value, dims="{}"):
    cache_execute(
        "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
        "VALUES (:cluster_id, :ts::timestamptz, :metric_type, :value, :dims::jsonb) "
        "ON CONFLICT DO NOTHING",
        {"cluster_id": cluster_id, "ts": ts, "metric_type": metric_type,
         "value": float(value), "dims": dims})


def collect_dynamodb_metrics(cw, dynamo, cache_execute, cluster_id, table_name, account_id, region):
    end = datetime.utcnow()
    start = end - timedelta(minutes=10)
    inserted = 0
    errors = []

    # Fix 2: initialize to None so a describe_table failure leaves billing_mode=None,
    # which naturally skips the Provisioned* queries below (guard: == "PROVISIONED").
    billing_mode = None
    gsi_list = []  # populated inside the try block; empty on describe_table failure
    try:
        t = dynamo.describe_table(TableName=table_name)["Table"]
        # A successful describe with no BillingModeSummary IS provisioned — keep that default.
        billing_mode = (t.get("BillingModeSummary") or {}).get("BillingMode", "PROVISIONED")
        # Attribute name -> type (S/N/B), so key schemas can show the data type.
        attrs = {a["AttributeName"]: a.get("AttributeType", "")
                 for a in t.get("AttributeDefinitions", [])}

        def _keys(schema):
            """KeySchema list -> {partition_key:{name,type}, sort_key:{name,type}|None}."""
            pk = sk = None
            for k in schema or []:
                info = {"name": k["AttributeName"], "type": attrs.get(k["AttributeName"], "")}
                if k.get("KeyType") == "HASH":
                    pk = info
                elif k.get("KeyType") == "RANGE":
                    sk = info
            return {"partition_key": pk, "sort_key": sk}

        details = {
            "billing_mode": billing_mode,
            "item_count": t.get("ItemCount", 0),
            "table_size_bytes": t.get("TableSizeBytes", 0),
            "table_status": t.get("TableStatus", ""),
            # Table class: "STANDARD" or "STANDARD_INFREQUENT_ACCESS".
            "table_class": (t.get("TableClassSummary") or {}).get("TableClass", "STANDARD"),
            # Global table replica regions (non-empty ⟹ this is a global table).
            "global_table_replicas": [
                r.get("RegionName") for r in (t.get("Replicas") or [])
                if r.get("RegionName")
            ],
            # Primary key (PK + optional SK) with attribute types.
            "key_schema": _keys(t.get("KeySchema")),
            # Global secondary indexes: own keys, projection, status, size.
            "gsi": [{
                "name": g.get("IndexName"),
                **_keys(g.get("KeySchema")),
                "projection": (g.get("Projection") or {}).get("ProjectionType", ""),
                "projection_attrs": (g.get("Projection") or {}).get("NonKeyAttributes", []),
                "status": g.get("IndexStatus", ""),
                "item_count": g.get("ItemCount", 0),
                "size_bytes": g.get("IndexSizeBytes", 0),
            } for g in t.get("GlobalSecondaryIndexes", [])],
            # Local secondary indexes: share the table PK, own sort key + projection.
            "lsi": [{
                "name": x.get("IndexName"),
                **_keys(x.get("KeySchema")),
                "projection": (x.get("Projection") or {}).get("ProjectionType", ""),
                "projection_attrs": (x.get("Projection") or {}).get("NonKeyAttributes", []),
            } for x in t.get("LocalSecondaryIndexes", [])],
        }
        # Capture GSI names for per-GSI metric collection below (no extra API call).
        gsi_list = t.get("GlobalSecondaryIndexes", [])

        # Fix 1a: include account_id + region to satisfy NOT NULL constraint on fresh rows.
        cache_execute(
            "INSERT INTO cluster_meta (cluster_id, account_id, region, engine, resource_details, updated_at) "
            "VALUES (:cid, :account_id, :region, 'dynamodb', :details::jsonb, NOW()) "
            "ON CONFLICT (cluster_id) DO UPDATE SET resource_details = EXCLUDED.resource_details, "
            "engine = 'dynamodb', updated_at = NOW()",
            {"cid": cluster_id, "account_id": account_id, "region": region, "details": json.dumps(details)})
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

    # --- per-GSI throttle + consumed metrics ---
    # Reuse the GSI list already fetched from describe_table (no extra API call).
    # gsi_list is [] when describe_table failed, so this loop is a safe no-op in that case.
    for gsi in gsi_list:
        gsi_name = gsi.get("IndexName")
        if not gsi_name:
            continue
        gsi_dims = table_dim + [{"Name": "GlobalSecondaryIndexName", "Value": gsi_name}]
        gsi_dim_json = json.dumps({"gsi": gsi_name})
        try:
            for metric, mtype in _GSI_METRICS_SUM:
                for dp in pull(metric, "Sum", gsi_dims):
                    if dp.get("Sum") is None:
                        continue
                    _insert(cache_execute, cluster_id, dp["Timestamp"].isoformat(),
                            mtype, dp["Sum"], gsi_dim_json)
                    inserted += 1
        except Exception as exc:
            errors.append(f"gsi {gsi_name}: {exc}")

    return {"cluster_id": cluster_id, "billing_mode": billing_mode,
            "metrics_inserted": inserted, "errors": errors}
