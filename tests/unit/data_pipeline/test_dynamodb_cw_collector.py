"""Unit tests for DynamoDB CloudWatch collector (TDD — write before implementation)."""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, call

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load(mod_name, rel):
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ddb = _load("dynamodb_cw_collector", "collectors/dynamodb_cw_collector.py")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DATAPOINT = {"Timestamp": datetime(2026, 6, 11), "Sum": 42.0, "Average": 42.0}

_ACCOUNT_ID = "123456789012"
_REGION = "ap-northeast-2"


def _make_cw(datapoint=_DATAPOINT):
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": [datapoint]}
    return cw


def _make_dynamo(billing_mode="PAY_PER_REQUEST"):
    dynamo = MagicMock()
    dynamo.describe_table.return_value = {
        "Table": {
            "BillingModeSummary": {"BillingMode": billing_mode},
            "ItemCount": 1000,
            "TableSizeBytes": 512000,
            "TableStatus": "ACTIVE",
            "GlobalSecondaryIndexes": [{"IndexName": "gsi-status"}],
        }
    }
    return dynamo


# ---------------------------------------------------------------------------
# Test 1 — PAY_PER_REQUEST: Sum metrics collected, Provisioned* NOT queried,
#           cluster_meta upsert includes account_id, region, resource_details
# ---------------------------------------------------------------------------

def test_collects_consumed_capacity_as_sum_and_inserts():
    cw = _make_cw()
    dynamo = _make_dynamo("PAY_PER_REQUEST")
    calls = []

    def cache_execute(sql, params):
        calls.append((sql, params))

    result = ddb.collect_dynamodb_metrics(
        cw, dynamo, cache_execute, "c1", "my-table", _ACCOUNT_ID, _REGION
    )

    # ConsumedReadCapacityUnits must be queried with Statistics=["Sum"]
    queried_metrics = {
        kw["MetricName"]: kw["Statistics"]
        for c in cw.get_metric_statistics.call_args_list
        for kw in [c.kwargs if c.kwargs else c[1]]
    }
    assert "ConsumedReadCapacityUnits" in queried_metrics
    assert queried_metrics["ConsumedReadCapacityUnits"] == ["Sum"]

    # ProvisionedReadCapacityUnits must NOT be queried for PAY_PER_REQUEST
    assert "ProvisionedReadCapacityUnits" not in queried_metrics

    # cluster_meta upsert must reference account_id, region, and resource_details
    meta_sqls = [sql for sql, _ in calls if "cluster_meta" in sql]
    assert meta_sqls, "Expected a cluster_meta upsert call"
    assert any("resource_details" in sql for sql in meta_sqls)
    assert any("account_id" in sql for sql in meta_sqls), \
        "cluster_meta INSERT must include account_id column"
    assert any("region" in sql for sql in meta_sqls), \
        "cluster_meta INSERT must include region column"

    # Params must carry the actual account_id and region values
    meta_params = [p for sql, p in calls if "cluster_meta" in sql]
    assert any(p.get("account_id") == _ACCOUNT_ID for p in meta_params), \
        "account_id param not passed to cluster_meta upsert"
    assert any(p.get("region") == _REGION for p in meta_params), \
        "region param not passed to cluster_meta upsert"

    # At least one metric row was inserted
    assert result["metrics_inserted"] > 0
    assert result["billing_mode"] == "PAY_PER_REQUEST"
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# Test 2 — PROVISIONED table: ProvisionedRead/WriteCapacityUnits ARE queried
# ---------------------------------------------------------------------------

def test_provisioned_table_queries_provisioned_metrics():
    cw = _make_cw()
    dynamo = _make_dynamo("PROVISIONED")

    result = ddb.collect_dynamodb_metrics(
        cw, dynamo, lambda sql, p: None, "c2", "prov-table", _ACCOUNT_ID, _REGION
    )

    queried_metrics = {
        kw["MetricName"]
        for c in cw.get_metric_statistics.call_args_list
        for kw in [c.kwargs if c.kwargs else c[1]]
    }
    assert "ProvisionedReadCapacityUnits" in queried_metrics
    assert "ProvisionedWriteCapacityUnits" in queried_metrics
    assert result["billing_mode"] == "PROVISIONED"


# ---------------------------------------------------------------------------
# Test 3 — SuccessfulRequestLatency always uses an Operation dimension
# ---------------------------------------------------------------------------

def test_latency_uses_operation_dimension():
    cw = _make_cw()
    dynamo = _make_dynamo("PAY_PER_REQUEST")

    ddb.collect_dynamodb_metrics(
        cw, dynamo, lambda sql, p: None, "c3", "lat-table", _ACCOUNT_ID, _REGION
    )

    latency_calls = [
        c for c in cw.get_metric_statistics.call_args_list
        if (c.kwargs if c.kwargs else c[1]).get("MetricName") == "SuccessfulRequestLatency"
    ]
    assert latency_calls, "SuccessfulRequestLatency must be queried"

    ops_seen = set()
    for c in latency_calls:
        kw = c.kwargs if c.kwargs else c[1]
        dims = {d["Name"]: d["Value"] for d in kw["Dimensions"]}
        assert "Operation" in dims, f"Missing Operation dimension in call: {kw}"
        ops_seen.add(dims["Operation"])

    # All four standard ops must be covered
    assert ops_seen >= {"GetItem", "Query", "PutItem", "Scan"}


# ---------------------------------------------------------------------------
# Test 4 — describe_table failure must NOT fire Provisioned* CW queries
# ---------------------------------------------------------------------------

def test_describe_failure_skips_provisioned_queries():
    """When describe_table raises, billing_mode stays None and no Provisioned*
    metrics should be queried. The function must still return without raising."""
    cw = _make_cw()
    dynamo = MagicMock()
    dynamo.describe_table.side_effect = Exception("AccessDenied")

    errors_seen = []

    def cache_execute(sql, params):
        pass

    result = ddb.collect_dynamodb_metrics(
        cw, dynamo, cache_execute, "c4", "fail-table", _ACCOUNT_ID, _REGION
    )

    # Must return a dict (not raise)
    assert isinstance(result, dict)

    # The describe error must be recorded
    assert result["errors"], "Expected at least one error recorded"
    assert any("describe_table" in e for e in result["errors"])

    # Provisioned* MetricNames must NOT appear in any CW call
    provisioned_metrics_queried = [
        (c.kwargs if c.kwargs else c[1])["MetricName"]
        for c in cw.get_metric_statistics.call_args_list
        if (c.kwargs if c.kwargs else c[1])["MetricName"]
        in {"ProvisionedReadCapacityUnits", "ProvisionedWriteCapacityUnits"}
    ]
    assert provisioned_metrics_queried == [], (
        f"Provisioned* metrics should NOT be queried when describe_table fails, "
        f"but got: {provisioned_metrics_queried}"
    )


# ---------------------------------------------------------------------------
# Test 5 — resource_details captures key schema (PK/SK), rich GSI, and LSI
# ---------------------------------------------------------------------------

def test_resource_details_captures_key_schema_gsi_lsi():
    import json

    cw = _make_cw()
    dynamo = MagicMock()
    dynamo.describe_table.return_value = {
        "Table": {
            "BillingModeSummary": {"BillingMode": "PROVISIONED"},
            "ItemCount": 5, "TableSizeBytes": 99, "TableStatus": "ACTIVE",
            "AttributeDefinitions": [
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "N"},
                {"AttributeName": "gpk", "AttributeType": "S"},
                {"AttributeName": "lsk", "AttributeType": "S"},
            ],
            "KeySchema": [
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            "GlobalSecondaryIndexes": [{
                "IndexName": "by-gpk",
                "KeySchema": [{"AttributeName": "gpk", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["a", "b"]},
                "IndexStatus": "ACTIVE", "ItemCount": 3, "IndexSizeBytes": 42,
            }],
            "LocalSecondaryIndexes": [{
                "IndexName": "by-lsk",
                "KeySchema": [
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "lsk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
        }
    }
    captured = {}

    def cache_execute(sql, params):
        if "cluster_meta" in sql:
            captured["details"] = json.loads(params["details"])

    ddb.collect_dynamodb_metrics(cw, dynamo, cache_execute, "c5", "rich", _ACCOUNT_ID, _REGION)
    d = captured["details"]

    # Primary key schema with types
    assert d["key_schema"]["partition_key"] == {"name": "pk", "type": "S"}
    assert d["key_schema"]["sort_key"] == {"name": "sk", "type": "N"}

    # GSI: name, own keys, projection (+ included attrs), status, size
    g = d["gsi"][0]
    assert g["name"] == "by-gpk"
    assert g["partition_key"] == {"name": "gpk", "type": "S"}
    assert g["sort_key"] is None
    assert g["projection"] == "INCLUDE"
    assert g["projection_attrs"] == ["a", "b"]
    assert g["status"] == "ACTIVE"

    # LSI: shares table PK, own sort key, projection
    lsi0 = d["lsi"][0]
    assert lsi0["name"] == "by-lsk"
    assert lsi0["sort_key"] == {"name": "lsk", "type": "S"}
    assert lsi0["projection"] == "ALL"


# ---------------------------------------------------------------------------
# Test 6 — per-GSI throttle/consumed metrics collected with GSI dimension
# ---------------------------------------------------------------------------

def test_gsi_metrics_collected_with_gsi_dimension():
    """When describe_table returns a GSI, the collector must issue CW calls
    with a GlobalSecondaryIndexName dimension and insert metric_snapshots
    rows with dimensions = {"gsi": <gsiname>}."""
    import json

    gsi_name = "gsi-status"
    cw = MagicMock()

    # All CW calls return one datapoint
    dp = {"Timestamp": datetime(2026, 6, 12), "Sum": 5.0, "Average": 5.0}
    cw.get_metric_statistics.return_value = {"Datapoints": [dp]}

    dynamo = MagicMock()
    dynamo.describe_table.return_value = {
        "Table": {
            "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
            "ItemCount": 100,
            "TableSizeBytes": 4096,
            "TableStatus": "ACTIVE",
            "AttributeDefinitions": [
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": gsi_name,
                    "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                    "IndexStatus": "ACTIVE",
                    "ItemCount": 50,
                    "IndexSizeBytes": 2048,
                }
            ],
        }
    }

    gsi_inserts = []

    def cache_execute(sql, params):
        if "metric_snapshots" in sql:
            dims_raw = params.get("dims", "{}")
            if dims_raw != "{}":
                import json as _json
                parsed = _json.loads(dims_raw)
                # Only collect rows that carry a "gsi" key (not "operation")
                if "gsi" in parsed:
                    gsi_inserts.append(params)

    ddb.collect_dynamodb_metrics(
        cw, dynamo, cache_execute, "c6", "gsi-table", _ACCOUNT_ID, _REGION
    )

    # At least one CW call must carry GlobalSecondaryIndexName dimension
    gsi_cw_calls = [
        c for c in cw.get_metric_statistics.call_args_list
        if any(
            d.get("Name") == "GlobalSecondaryIndexName"
            for d in (c.kwargs if c.kwargs else c[1]).get("Dimensions", [])
        )
    ]
    assert gsi_cw_calls, "Expected CW calls with GlobalSecondaryIndexName dimension"

    # Each such call must have GlobalSecondaryIndexName = gsi_name
    for cw_call in gsi_cw_calls:
        kw = cw_call.kwargs if cw_call.kwargs else cw_call[1]
        dim_map = {d["Name"]: d["Value"] for d in kw["Dimensions"]}
        assert dim_map.get("GlobalSecondaryIndexName") == gsi_name, (
            f"Expected GSI name {gsi_name!r}, got {dim_map}"
        )
        assert dim_map.get("TableName") == "gsi-table"

    # GSI metric inserts must have dims = {"gsi": gsi_name}
    assert gsi_inserts, "Expected at least one metric_snapshots insert with GSI dims"
    for row in gsi_inserts:
        dims = json.loads(row["dims"])
        assert dims.get("gsi") == gsi_name, f"Expected gsi dim {gsi_name!r}, got {dims}"

    # The GSI metric_types must be one of the four expected types
    gsi_metric_types = {row["metric_type"] for row in gsi_inserts}
    expected_types = {"consumed_rcu", "consumed_wcu", "read_throttle_events", "write_throttle_events"}
    assert gsi_metric_types <= expected_types, (
        f"Unexpected GSI metric types: {gsi_metric_types - expected_types}"
    )


# ---------------------------------------------------------------------------
# Test 7 — GSI collection failure does NOT break table-level metrics
# ---------------------------------------------------------------------------

def test_gsi_failure_does_not_break_table_metrics():
    """If a per-GSI CW call raises, table-level metrics are still collected
    and the function returns without raising."""
    cw = MagicMock()
    dp = {"Timestamp": datetime(2026, 6, 12), "Sum": 10.0, "Average": 10.0}

    def cw_side_effect(**kwargs):
        dims = {d["Name"]: d["Value"] for d in kwargs.get("Dimensions", [])}
        # Fail only GSI calls
        if "GlobalSecondaryIndexName" in dims:
            raise Exception("ThrottlingException: GSI call failed")
        return {"Datapoints": [dp]}

    cw.get_metric_statistics.side_effect = cw_side_effect

    dynamo = MagicMock()
    dynamo.describe_table.return_value = {
        "Table": {
            "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
            "ItemCount": 10,
            "TableSizeBytes": 1024,
            "TableStatus": "ACTIVE",
            "GlobalSecondaryIndexes": [{"IndexName": "bad-gsi"}],
        }
    }

    result = ddb.collect_dynamodb_metrics(
        cw, dynamo, lambda sql, params: None, "c7", "safe-table", _ACCOUNT_ID, _REGION
    )

    # Must return without raising
    assert isinstance(result, dict)
    # Table-level metrics must still be inserted
    assert result["metrics_inserted"] > 0
