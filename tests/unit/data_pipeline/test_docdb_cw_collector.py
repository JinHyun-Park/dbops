"""Unit tests for DocumentDB CloudWatch collector (TDD — write before implementation)."""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load(mod_name, rel):
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


docdb = _load("docdb_cw_collector", "collectors/docdb_cw_collector.py")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DATAPOINT = {"Timestamp": datetime(2026, 6, 11), "Average": 5.0, "Sum": 5.0}


def _make_cw(datapoint=_DATAPOINT):
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": [datapoint]}
    return cw


def _make_docdb(writer_id="docdb-1-writer"):
    client = MagicMock()
    client.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "DBClusterIdentifier": "my-docdb-cluster",
                "EngineVersion": "5.0.0",
                "Status": "available",
                "DBClusterMembers": [
                    {"DBInstanceIdentifier": writer_id, "IsClusterWriter": True},
                    {"DBInstanceIdentifier": "docdb-1-reader", "IsClusterWriter": False},
                ],
            }
        ]
    }
    return client


# ---------------------------------------------------------------------------
# Test 1 — namespace is AWS/DocDB, writer instance dim used for instance metrics,
#           cluster dim used for cluster metrics, meta upsert includes resource_details
# ---------------------------------------------------------------------------

def test_uses_docdb_namespace_and_writer_instance_dim():
    cw = _make_cw()
    client = _make_docdb("docdb-1-writer")
    cache_calls = []

    def cache_execute(sql, params):
        cache_calls.append((sql, params))

    result = docdb.collect_docdb_metrics(
        cw, client, cache_execute, "my-docdb-cluster", "us-east-1", "123456789012"
    )

    # Every CW call must use Namespace="AWS/DocDB"
    all_cw_calls = cw.get_metric_statistics.call_args_list
    assert all_cw_calls, "Expected at least one get_metric_statistics call"
    for c in all_cw_calls:
        kw = c.kwargs if c.kwargs else c[1]
        assert kw["Namespace"] == "AWS/DocDB", f"Wrong namespace: {kw['Namespace']}"

    # Collect dimension names used across all calls
    dims_by_call = [
        {d["Name"]: d["Value"] for d in (c.kwargs if c.kwargs else c[1])["Dimensions"]}
        for c in all_cw_calls
    ]

    # At least one call must use DBInstanceIdentifier=docdb-1-writer (instance-scoped)
    instance_calls = [d for d in dims_by_call if "DBInstanceIdentifier" in d]
    assert instance_calls, "Expected at least one instance-scoped CW call"
    assert any(
        d["DBInstanceIdentifier"] == "docdb-1-writer" for d in instance_calls
    ), "Writer instance ID not used in any instance-scoped call"

    # At least one call must use DBClusterIdentifier (cluster-scoped)
    cluster_calls = [d for d in dims_by_call if "DBClusterIdentifier" in d]
    assert cluster_calls, "Expected at least one cluster-scoped CW call"

    # Metrics must have been inserted
    assert result["metrics_inserted"] > 0, "Expected metrics_inserted > 0"
    assert result["writer"] == "docdb-1-writer"
    assert result["errors"] == []

    # cluster_meta upsert must contain account_id, region, resource_details, engine='docdb'
    meta_sqls = [sql for sql, _ in cache_calls if "cluster_meta" in sql]
    assert meta_sqls, "Expected a cluster_meta upsert call"
    assert any("resource_details" in sql for sql in meta_sqls)
    assert any("engine='docdb'" in sql for sql in meta_sqls)
    assert any("account_id" in sql for sql in meta_sqls), \
        "cluster_meta INSERT must include account_id column"
    assert any("region" in sql for sql in meta_sqls), \
        "cluster_meta INSERT must include region column"

    # Params must carry the actual account_id and region values
    meta_params = [p for sql, p in cache_calls if "cluster_meta" in sql]
    assert any(p.get("account_id") == "123456789012" for p in meta_params), \
        "account_id param not passed to cluster_meta upsert"
    assert any(p.get("region") == "us-east-1" for p in meta_params), \
        "region param not passed to cluster_meta upsert"


# ---------------------------------------------------------------------------
# Test 2 — empty DBClusterMembers: no instance-scoped metrics queried
# ---------------------------------------------------------------------------

def test_no_writer_skips_instance_metrics():
    cw = _make_cw()
    client = MagicMock()
    client.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "DBClusterIdentifier": "empty-cluster",
                "EngineVersion": "5.0.0",
                "Status": "available",
                "DBClusterMembers": [],
            }
        ]
    }

    result = docdb.collect_docdb_metrics(
        cw, client, lambda sql, p: None, "empty-cluster", "us-east-1", "123456789012"
    )

    # With no members → writer is None → no DBInstanceIdentifier dimension used
    all_cw_calls = cw.get_metric_statistics.call_args_list
    for c in all_cw_calls:
        kw = c.kwargs if c.kwargs else c[1]
        dims = {d["Name"]: d["Value"] for d in kw["Dimensions"]}
        assert "DBInstanceIdentifier" not in dims, (
            f"Instance-scoped call made despite no members: {kw}"
        )

    assert result["writer"] is None
    # Cluster-scoped metrics may still be queried
