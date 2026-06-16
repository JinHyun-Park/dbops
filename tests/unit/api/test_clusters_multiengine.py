"""Tests for multi-engine cluster registration (Task 7).

Covers:
  1. DynamoDB table registration — slug cluster_id, no secret, no RDS call.
  2. DocDB cluster registration — docdb API, no secret, no RDS call.
  3. Aurora registration — existing path unchanged (engine defaults, cluster_id
     preserved).
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_CLUSTERS_DIR = Path(__file__).resolve().parents[3] / "api" / "clusters"
# Push clusters/ dir so `import seeder` and `import engine_family` both resolve.
sys.path.insert(0, str(_CLUSTERS_DIR))

_PATH = _CLUSTERS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("clusters_handler_me", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import pytest


@pytest.fixture(autouse=True)
def _clusters_table_env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_table():
    t = MagicMock()
    t.put_item = MagicMock()
    return t


# ---------------------------------------------------------------------------
# Test 1 — DynamoDB registration
# ---------------------------------------------------------------------------

@patch.object(handler, "_rds_client_for")
@patch.object(handler, "_ddb_client_for")
def test_register_dynamodb_uses_slug_and_describe_table(mock_ddb_for, mock_rds_for):
    """_handle_register with engine='dynamodb' must:
    - call describe_table (connectivity check)
    - store a ddb-* cluster_id slug
    - set engine='dynamodb', engine_family='dynamodb'
    - store resource_name='Orders'
    - NOT store secret_arn
    - NEVER call _rds_client_for
    """
    mock_ddb_client = MagicMock()
    mock_ddb_client.describe_table.return_value = {"Table": {"TableName": "Orders"}}
    mock_ddb_for.return_value = mock_ddb_client

    table = _mock_table()
    resp = handler._handle_register(table, {
        "engine": "dynamodb",
        "account_id": "123456789012",
        "region": "ap-northeast-2",
        "resource_name": "Orders",
    })

    assert resp["statusCode"] in (201, 207)
    body = json.loads(resp["body"])
    assert body["connection_status"] == "ok"
    assert body["cluster_id"].startswith("ddb-")

    put_call = table.put_item.call_args
    assert put_call is not None, "table.put_item was not called"
    item = put_call.kwargs.get("Item") or put_call.args[0].get("Item") if put_call.args else put_call.kwargs["Item"]
    # Check via the call kwargs directly
    item = table.put_item.call_args[1]["Item"]

    assert item["engine"] == "dynamodb"
    assert item["engine_family"] == "dynamodb"
    assert item["cluster_id"].startswith("ddb-")
    assert item["resource_name"] == "Orders"
    assert "secret_arn" not in item, "DynamoDB items must NOT have secret_arn"

    # Aurora path must have been completely bypassed
    mock_rds_for.assert_not_called()


@patch.object(handler, "_rds_client_for")
@patch.object(handler, "_ddb_client_for")
def test_register_dynamodb_missing_resource_name_400(mock_ddb_for, mock_rds_for):
    """resource_name is required for DynamoDB registration."""
    table = _mock_table()
    resp = handler._handle_register(table, {
        "engine": "dynamodb",
        "account_id": "123456789012",
        "region": "ap-northeast-2",
        # resource_name omitted
    })
    assert resp["statusCode"] == 400
    mock_rds_for.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2 — DocumentDB registration
# ---------------------------------------------------------------------------

@patch.object(handler, "_rds_client_for")
@patch.object(handler, "_docdb_client_for")
def test_register_docdb_uses_docdb_api(mock_docdb_for, mock_rds_for):
    """_handle_register with engine='docdb' must:
    - call describe_db_clusters via docdb client
    - store engine='docdb', engine_family='documentdb'
    - NOT store secret_arn
    - NEVER call _rds_client_for
    """
    mock_docdb_client = MagicMock()
    mock_docdb_client.describe_db_clusters.return_value = {
        "DBClusters": [{"EngineVersion": "5.0.0"}]
    }
    mock_docdb_for.return_value = mock_docdb_client

    table = _mock_table()
    resp = handler._handle_register(table, {
        "engine": "docdb",
        "cluster_id": "my-docdb-cluster",
        "account_id": "123456789012",
        "region": "us-east-1",
    })

    assert resp["statusCode"] in (201, 207)
    body = json.loads(resp["body"])
    assert body["connection_status"] == "ok"
    assert body["cluster_id"] == "my-docdb-cluster"

    item = table.put_item.call_args[1]["Item"]
    assert item["engine"] == "docdb"
    assert item["engine_family"] == "documentdb"
    assert item["cluster_id"] == "my-docdb-cluster"
    assert "secret_arn" not in item, "DocDB items must NOT have secret_arn"

    mock_rds_for.assert_not_called()


@patch.object(handler, "_rds_client_for")
@patch.object(handler, "_docdb_client_for")
def test_register_docdb_connectivity_failure_returns_207(mock_docdb_for, mock_rds_for):
    """DocDB register with describe_db_clusters failure → 207 + registered_with_warning."""
    mock_docdb_client = MagicMock()
    mock_docdb_client.describe_db_clusters.side_effect = Exception("AccessDenied")
    mock_docdb_for.return_value = mock_docdb_client

    table = _mock_table()
    resp = handler._handle_register(table, {
        "engine": "docdb",
        "cluster_id": "bad-cluster",
        "account_id": "123456789012",
        "region": "us-east-1",
    })

    assert resp["statusCode"] == 207
    body = json.loads(resp["body"])
    assert body["status"] == "registered_with_warning"
    assert body["connection_status"] == "failed"

    item = table.put_item.call_args[1]["Item"]
    assert item["connection_status"] == "failed"
    assert "AccessDenied" in item["connection_error"]


# ---------------------------------------------------------------------------
# Test 3 — Aurora path unchanged
# ---------------------------------------------------------------------------

@patch.object(handler, "_rds_client_for")
def test_register_aurora_unchanged(mock_rds_for):
    """Registering without engine (Aurora default) must use the existing
    RDS path, preserve cluster_id, and default engine to aurora-postgresql."""
    mock_rds_client = MagicMock()
    mock_rds_client.describe_db_clusters.return_value = {
        "DBClusters": [{
            "DBClusterArn": "arn:aws:rds:ap-northeast-2:123456789012:cluster:prod-pg",
            "Engine": "aurora-postgresql",
            "EngineVersion": "15.4",
            "MasterUserSecret": {"SecretArn": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:master"},
            "DatabaseName": "mydb",
        }]
    }
    mock_rds_for.return_value = mock_rds_client

    table = _mock_table()
    resp = handler._handle_register(table, {
        "cluster_id": "prod-pg",
        "account_id": "123456789012",
        "region": "ap-northeast-2",
        # no engine supplied → default aurora-postgresql
    })

    assert resp["statusCode"] == 201
    body = json.loads(resp["body"])
    assert body["cluster_id"] == "prod-pg"
    assert body["connection_status"] == "ok"

    item = table.put_item.call_args[1]["Item"]
    assert item["cluster_id"] == "prod-pg"
    assert item["engine"] == "aurora-postgresql"
    # Aurora path MUST set secret_arn from RDS response
    assert item.get("secret_arn") == "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:master"


# ---------------------------------------------------------------------------
# Test 4 — bulk-register with a DynamoDB entry (resource_name threaded)
# ---------------------------------------------------------------------------

@patch.object(handler, "_rds_client_for")
@patch.object(handler, "_ddb_client_for")
def test_bulk_register_dynamodb_succeeds_with_ddb_slug(mock_ddb_for, mock_rds_for):
    """_handle_bulk_register must pass resource_name to _register_dynamodb so
    the DynamoDB path succeeds. The registered cluster_id must start with 'ddb-'.
    """
    mock_ddb_client = MagicMock()
    mock_ddb_client.describe_table.return_value = {"Table": {"TableName": "Orders"}}
    mock_ddb_for.return_value = mock_ddb_client

    table = _mock_table()
    # Simulate: table has no existing item (not yet registered)
    table.get_item.return_value = {}

    bulk_body = {
        "clusters": [
            {
                "cluster_id": "ddb-placeholder",  # will be recomputed inside _register_dynamodb
                "account_id": "123456789012",
                "region": "ap-northeast-2",
                "engine": "dynamodb",
                "resource_name": "Orders",
            }
        ]
    }
    resp = handler._handle_bulk_register(table, bulk_body)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])

    assert body["counts"]["failed"] == 0, f"unexpected failures: {body.get('failed')}"
    assert body["counts"]["registered"] == 1
    assert len(body["registered"]) == 1
    assert body["registered"][0]["connection_status"] == "ok"

    # The DynamoDB put_item must have been called with a ddb-* cluster_id
    put_call = table.put_item.call_args[1]["Item"]
    assert put_call["cluster_id"].startswith("ddb-")
    assert put_call["resource_name"] == "Orders"
    mock_rds_for.assert_not_called()


@patch.object(handler, "_rds_client_for")
@patch.object(handler, "_ddb_client_for")
def test_bulk_register_dynamodb_slug_matches_discovery(mock_ddb_for, mock_rds_for):
    """Discovery and bulk-register must produce the same ddb-* cluster_id when
    account_id is threaded into _list_clusters_in_region (account-threading approach).

    This test simulates:
      1. Discovery with account_id='123456789012' → discover the 'Orders' table.
      2. bulk-register that exact entry → _register_dynamodb recomputes with same account_id.
      3. Both must produce the same ddb-* slug.
    """
    from engine_family import dynamodb_cluster_id

    account_id = "123456789012"
    region = "ap-northeast-2"
    table_name = "Orders"

    # The slug discovery produces (after the fix: account_id threaded in)
    expected_id = dynamodb_cluster_id(account_id, region, table_name)
    assert expected_id.startswith("ddb-")

    # The slug _register_dynamodb produces with the same account_id
    registered_id = dynamodb_cluster_id(account_id, region, table_name)
    assert registered_id == expected_id, (
        f"Discovery id {expected_id!r} != registration id {registered_id!r} — "
        "account_id must be threaded into _list_clusters_in_region"
    )

    # Also verify that the OLD discovery slug (empty account_id) is DIFFERENT
    old_discovery_id = dynamodb_cluster_id("", region, table_name)
    assert old_discovery_id != expected_id, (
        "Regression: empty-account_id slug should differ from real-account_id slug"
    )


# ---------------------------------------------------------------------------
# Test 5 — discovery must not duplicate Aurora as fake DocumentDB
# ---------------------------------------------------------------------------

def _paginator(pages_key, items):
    pag = MagicMock()
    pag.paginate.return_value = [{pages_key: items}]
    return pag


@patch.object(handler, "_convention_secret_for", return_value="")
@patch.object(handler, "_session_for")
def test_discover_docdb_path_excludes_non_docdb(mock_session_for, _mock_secret):
    """Regression: the docdb client shares the RDS control plane, so
    docdb.describe_db_clusters returns Aurora / RDS / Neptune clusters too — not
    just DocumentDB. _list_clusters_in_region must keep only Engine=='docdb' from
    the docdb path; otherwise every Aurora cluster (already found via the rds
    paginator) is duplicated and mislabeled 'docdb', flooding discovery."""
    aurora = {
        "DBClusterIdentifier": "prod-pg",
        "DBClusterArn": "arn:aws:rds:ap-northeast-2:123456789012:cluster:prod-pg",
        "Engine": "aurora-postgresql",
        "EngineVersion": "15.4",
        "Status": "available",
        "DatabaseName": "mydb",
    }
    real_docdb = {
        "DBClusterIdentifier": "my-docdb",
        "Engine": "docdb",
        "EngineVersion": "5.0.0",
        "Status": "available",
    }

    rds_client = MagicMock()
    rds_client.get_paginator.return_value = _paginator("DBClusters", [aurora])
    dynamo_client = MagicMock()
    dynamo_client.get_paginator.return_value = _paginator("TableNames", [])
    # The docdb endpoint echoes the Aurora cluster back alongside the real one.
    docdb_client = MagicMock()
    docdb_client.get_paginator.return_value = _paginator("DBClusters", [aurora, real_docdb])

    clients = {"rds": rds_client, "dynamodb": dynamo_client, "docdb": docdb_client}
    session = MagicMock()
    session.client.side_effect = lambda svc, *a, **k: clients.get(svc, MagicMock())
    mock_session_for.return_value = session

    out = handler._list_clusters_in_region("ap-northeast-2", account_id="123456789012")

    aurora_entries = [c for c in out if c["engine"].startswith("aurora")]
    docdb_entries = [c for c in out if c.get("engine") == "docdb"]

    assert len(aurora_entries) == 1, f"Aurora should appear once, got {aurora_entries}"
    assert len(docdb_entries) == 1, f"only the real DocDB should appear, got {docdb_entries}"
    assert docdb_entries[0]["cluster_id"] == "my-docdb"
    # The specific regression: Aurora must NOT be duplicated as a docdb entry.
    assert not any(c.get("engine") == "docdb" and c["cluster_id"] == "prod-pg" for c in out), (
        "Aurora cluster leaked into discovery as a fake 'docdb' entry"
    )
