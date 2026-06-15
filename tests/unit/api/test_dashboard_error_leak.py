"""Regression: dashboard read endpoints must NOT return raw boto3 fault strings
to the client.

_schema_graph / _redundant_indexes / _table_indexes previously returned
`str(e)[:300]` in the error response, which can carry ARNs / account ids from a
boto3 ClientError. They now log the fault server-side and return a friendly
Korean message. These tests inject a fault containing a fake ARN + account id
and assert none of it leaks into the response.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DIR))

os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_spec = importlib.util.spec_from_file_location("dashboard_handler_leak", _DIR / "handler.py")
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

# A fault string carrying exactly the kind of sensitive identifiers we must not
# echo back to the browser.
SECRET_FAULT = (
    "ClientError: AccessDenied on "
    "arn:aws:rds:ap-northeast-2:999988887777:cluster:super-secret-prod"
)


@pytest.fixture
def relational(monkeypatch):
    """A registered relational cluster whose rds-data ExecuteStatement raises a
    fault carrying a fake ARN / account id."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")
    monkeypatch.setattr(
        handler,
        "_lookup_cluster",
        lambda cid: {
            "cluster_arn": "arn:aws:rds:ap-northeast-2:123:cluster:target",
            "secret_arn": "arn:aws:secretsmanager:ap-northeast-2:123:secret:target",
            "engine": "aurora-postgresql",
            "db_name": "postgres",
        },
    )

    def _raising_client(*_a, **_k):
        c = MagicMock()
        c.execute_statement.side_effect = RuntimeError(SECRET_FAULT)
        return c

    monkeypatch.setattr(handler.boto3, "client", _raising_client)


def _assert_no_leak(result):
    blob = str(result)
    assert SECRET_FAULT not in blob, "raw fault string leaked to client"
    assert "AccessDenied" not in blob, "raw fault leaked"
    assert "999988887777" not in blob, "account id leaked"
    assert "super-secret-prod" not in blob, "resource arn leaked"
    assert result.get("error") == "execution_failed"
    # Friendly Korean message in its place.
    assert "실패" in result.get("message", "")


def test_schema_graph_no_raw_fault_leak(relational):
    _assert_no_leak(handler._schema_graph("prod-pg", "public"))


def test_redundant_indexes_no_raw_fault_leak(relational):
    _assert_no_leak(handler._redundant_indexes("prod-pg"))


def test_table_indexes_no_raw_fault_leak(relational):
    _assert_no_leak(handler._table_indexes("prod-pg", "public", "orders"))
