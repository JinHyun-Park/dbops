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
