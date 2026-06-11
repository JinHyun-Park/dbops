"""Unit tests for the _resource_details endpoint (Task 13).

Tests:
  - Returns engine / engine_family / resource_details dict from cluster_meta.
  - Parses resource_details JSONB string into a dict.
  - Returns None resource_details when the column is None / absent.
  - Returns None fields when cluster_id not found.
  - Handles malformed JSON gracefully (returns None, not an exception).
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler_rd", _PATH)
handler = importlib.util.module_from_spec(_spec)

os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_spec.loader.exec_module(handler)

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_query(rows):
    """Return a query callable that always returns `rows`."""
    def _q(sql, params=None):
        return rows
    return _q


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_resource_details_returns_parsed_dict():
    """resource_details JSONB string must be parsed into a dict."""
    rd_payload = {"billing_mode": "PAY_PER_REQUEST", "item_count": 42000, "table_status": "ACTIVE"}
    rows = [{
        "engine": "dynamodb",
        "engine_family": "dynamodb",
        "resource_details": json.dumps(rd_payload),
    }]
    result = handler._resource_details(_make_query(rows), "my-ddb-table")

    assert result["cluster_id"] == "my-ddb-table"
    assert result["engine"] == "dynamodb"
    assert result["engine_family"] == "dynamodb"
    assert isinstance(result["resource_details"], dict)
    assert result["resource_details"]["billing_mode"] == "PAY_PER_REQUEST"
    assert result["resource_details"]["item_count"] == 42000


def test_resource_details_handles_dict_value():
    """If resource_details is already a dict (e.g. RDS Data API returns it parsed),
    it must be returned as-is without a double-parse."""
    rd_payload = {"instance_count": 3, "engine_version": "4.0.0"}
    rows = [{
        "engine": "docdb",
        "engine_family": "documentdb",
        "resource_details": rd_payload,  # already a dict
    }]
    result = handler._resource_details(_make_query(rows), "my-docdb-cluster")

    assert result["resource_details"]["instance_count"] == 3
    assert result["resource_details"]["engine_version"] == "4.0.0"


def test_resource_details_none_when_column_absent():
    """When resource_details column is None, return None (not an exception)."""
    rows = [{
        "engine": "aurora-postgresql",
        "engine_family": "relational",
        "resource_details": None,
    }]
    result = handler._resource_details(_make_query(rows), "prod-pg")

    assert result["engine"] == "aurora-postgresql"
    assert result["resource_details"] is None


def test_resource_details_not_found():
    """When cluster_id has no row in cluster_meta, all fields are None."""
    result = handler._resource_details(_make_query([]), "ghost-cluster")

    assert result["cluster_id"] == "ghost-cluster"
    assert result["engine"] is None
    assert result["engine_family"] is None
    assert result["resource_details"] is None


def test_resource_details_malformed_json():
    """Malformed JSON in resource_details must NOT raise — returns None."""
    rows = [{
        "engine": "dynamodb",
        "engine_family": "dynamodb",
        "resource_details": "{broken json",
    }]
    result = handler._resource_details(_make_query(rows), "bad-cluster")

    assert result["engine"] == "dynamodb"
    assert result["resource_details"] is None


def test_resource_details_docdb_engine_version_merged_from_column():
    """engine_version stored as a cluster_meta column (not inside JSONB) must be
    merged into resource_details so the DocDB panel can render it."""
    rd_payload = {"instance_count": 3, "instances": ["docdb-instance-1", "docdb-instance-2"]}
    rows = [{
        "engine": "docdb",
        "engine_version": "5.0.0",
        "resource_details": rd_payload,  # already a dict (parsed JSONB)
    }]
    result = handler._resource_details(_make_query(rows), "my-docdb")

    rd = result["resource_details"]
    assert rd is not None
    assert rd["engine_version"] == "5.0.0", "engine_version must be merged from column"


def test_resource_details_docdb_engine_version_not_overwritten():
    """If engine_version is already inside resource_details JSONB, the column
    value must NOT overwrite it."""
    rd_payload = {"instance_count": 2, "engine_version": "4.0.0"}
    rows = [{
        "engine": "docdb",
        "engine_version": "5.0.0",  # column says 5.0.0
        "resource_details": rd_payload,  # JSONB says 4.0.0 — keep this
    }]
    result = handler._resource_details(_make_query(rows), "my-docdb")

    rd = result["resource_details"]
    assert rd["engine_version"] == "4.0.0", "existing JSONB engine_version must not be overwritten"


def test_resource_details_docdb_instances_normalised_to_objects():
    """DocDB instances stored as plain strings must be normalised to
    {"instance_id": <str>} objects so the frontend panel can render them."""
    rd_payload = {
        "instance_count": 2,
        "instances": ["docdb-inst-1", "docdb-inst-2"],
    }
    rows = [{
        "engine": "docdb",
        "engine_version": "5.0.0",
        "resource_details": rd_payload,
    }]
    result = handler._resource_details(_make_query(rows), "my-docdb")

    instances = result["resource_details"]["instances"]
    assert len(instances) == 2
    for inst in instances:
        assert isinstance(inst, dict), "each instance must be a dict"
        assert "instance_id" in inst, "instance must have instance_id key"
    assert instances[0]["instance_id"] == "docdb-inst-1"
    assert instances[1]["instance_id"] == "docdb-inst-2"


def test_resource_details_docdb_instances_already_objects_unchanged():
    """If instances are already objects (e.g. from a newer collector), leave them
    as-is without wrapping further."""
    rd_payload = {
        "instance_count": 1,
        "instances": [{"instance_id": "docdb-inst-1", "status": "available"}],
    }
    rows = [{
        "engine": "docdb",
        "engine_version": "5.0.0",
        "resource_details": rd_payload,
    }]
    result = handler._resource_details(_make_query(rows), "my-docdb")

    instances = result["resource_details"]["instances"]
    assert instances[0]["instance_id"] == "docdb-inst-1"
    assert instances[0]["status"] == "available"


def test_resource_details_via_lambda_handler(monkeypatch):
    """lambda_handler must route /resource-details to _resource_details and
    return 200 with the correct JSON body."""
    rd_payload = {"billing_mode": "PROVISIONED", "item_count": 100}
    fake_row = {
        "engine": "dynamodb",
        "engine_family": "dynamodb",
        "resource_details": json.dumps(rd_payload),
    }

    # Patch the rds-data client so execute_statement returns our fake row
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = {
        "columnMetadata": [
            {"name": "engine", "typeName": "varchar"},
            {"name": "engine_family", "typeName": "varchar"},
            {"name": "resource_details", "typeName": "jsonb"},
        ],
        "records": [
            [
                {"stringValue": "dynamodb"},
                {"stringValue": "dynamodb"},
                {"stringValue": json.dumps(rd_payload)},
            ]
        ],
    }
    monkeypatch.setattr(handler, "_rds_data", lambda: mock_rds)

    event = {
        "rawPath": "/api/dashboard/my-ddb-table/resource-details",
        "pathParameters": {"cluster_id": "my-ddb-table"},
        "queryStringParameters": {},
        "headers": {},
    }
    resp = handler.lambda_handler(event, {})

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["engine"] == "dynamodb"
    rd = body["resource_details"]
    assert isinstance(rd, dict)
    assert rd["billing_mode"] == "PROVISIONED"
