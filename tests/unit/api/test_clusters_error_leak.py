"""Regression: a FAILED cluster registration must not persist or return raw AWS
exception text.

Every registration path used to do `except Exception as e: status, err =
"failed", str(e)[:300]` and store that into the registry row's
`connection_error`. That field is the worst place for it:

  * GET /api/clusters returns registry rows UNPROJECTED, and tenancy filters
    WHICH clusters a caller sees, not WHICH fields. So one admin's failed
    registration handed the hub account id and the platform IAM role name to
    every viewer who could see that cluster.
  * An AWS describe / AssumeRole error spells out exactly those identifiers.

The paths now store a classified AWS error CODE mapped to a static Korean
reason (`_conn_error`) and log the full exception to CloudWatch. These tests
inject a fault carrying a fake hub account id, platform role name and target ARN
and assert none of it reaches the stored row or the GET projection, while
`connection_status` semantics ("failed" + 207) stay unchanged.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

_CLUSTERS_DIR = Path(__file__).resolve().parents[3] / "api" / "clusters"
sys.path.insert(0, str(_CLUSTERS_DIR))

_spec = importlib.util.spec_from_file_location(
    "clusters_handler_leak", _CLUSTERS_DIR / "handler.py"
)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


# The identifiers a real AWS error carries. None of these may appear in a
# response or in the persisted row.
HUB_ACCOUNT = "999988887777"
PLATFORM_ROLE = "dbops-prod-mcp-operations-role"
TARGET_ARN = f"arn:aws:rds:ap-northeast-2:{HUB_ACCOUNT}:cluster:super-secret-prod"
SECRET_FAULT = (
    f"User: arn:aws:sts::{HUB_ACCOUNT}:assumed-role/{PLATFORM_ROLE}/session is not "
    f"authorized to perform: rds:DescribeDBClusters on resource: {TARGET_ARN}"
)


def _client_error():
    """A botocore ClientError shaped like the real AccessDenied, message and all."""
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": SECRET_FAULT}},
        "DescribeDBClusters",
    )


class _FakeTable:
    """DynamoDB table double with REAL dict storage, so the test can read back
    exactly what registration persisted. A MagicMock's put_item is a no-op and
    would hide a leak that is stored but never returned."""

    def __init__(self):
        self.items: dict = {}

    def get_item(self, Key):
        item = self.items.get(Key["cluster_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["cluster_id"]] = dict(Item)

    def scan(self, **kwargs):
        return {"Items": [dict(v) for v in self.items.values()]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    # _enrich_with_meta would try to reach the cache DB over rds-data; the
    # registry row is what these tests are about.
    monkeypatch.setattr(handler, "_enrich_with_meta", lambda rows: rows)


def _assert_clean(blob: str, where: str):
    assert SECRET_FAULT not in blob, f"raw fault leaked into {where}"
    assert HUB_ACCOUNT not in blob, f"hub account id leaked into {where}"
    assert PLATFORM_ROLE not in blob, f"platform role name leaked into {where}"
    assert TARGET_ARN not in blob, f"target ARN leaked into {where}"
    assert "assumed-role" not in blob, f"role session leaked into {where}"
    assert "not authorized to perform" not in blob, f"fault text leaked into {where}"


# ---------------------------------------------------------------------------
# One case per registration path. Each returns (table, response).
# ---------------------------------------------------------------------------

def _register_dynamodb(monkeypatch):
    table = _FakeTable()
    monkeypatch.setattr(
        handler, "_ddb_client_for",
        lambda *_a, **_k: MagicMock(describe_table=MagicMock(side_effect=_client_error())),
    )
    resp = handler._handle_register(table, {
        "engine": "dynamodb", "account_id": "111122223333",
        "region": "ap-northeast-2", "resource_name": "orders",
    })
    return table, resp


def _register_docdb(monkeypatch):
    table = _FakeTable()
    monkeypatch.setattr(
        handler, "_docdb_client_for",
        lambda *_a, **_k: MagicMock(
            describe_db_clusters=MagicMock(side_effect=_client_error())),
    )
    resp = handler._handle_register(table, {
        "engine": "docdb", "cluster_id": "docdb-1",
        "account_id": "111122223333", "region": "ap-northeast-2",
    })
    return table, resp


def _register_elasticache(monkeypatch):
    table = _FakeTable()
    # Both describe paths fail: the replication-group probe AND the
    # cache-cluster fallback, which is the branch that stored str(e).
    monkeypatch.setattr(
        handler, "_elasticache_client_for",
        lambda *_a, **_k: MagicMock(
            describe_replication_groups=MagicMock(side_effect=_client_error()),
            describe_cache_clusters=MagicMock(side_effect=_client_error()),
        ),
    )
    resp = handler._handle_register(table, {
        "engine": "redis", "account_id": "111122223333",
        "region": "ap-northeast-2", "resource_name": "cache-1",
    })
    return table, resp


def _register_aurora(monkeypatch):
    """The relational path the review did NOT name, and the default engine."""
    table = _FakeTable()
    monkeypatch.setattr(
        handler, "_rds_client_for",
        lambda *_a, **_k: MagicMock(
            describe_db_clusters=MagicMock(side_effect=_client_error())),
    )
    resp = handler._handle_register(table, {
        "engine": "aurora-postgresql", "cluster_id": "prod-pg",
        "account_id": "111122223333", "region": "ap-northeast-2",
    })
    return table, resp


_PATHS = {
    "dynamodb": _register_dynamodb,
    "docdb": _register_docdb,
    "elasticache": _register_elasticache,
    "aurora": _register_aurora,
}


@pytest.mark.parametrize("family", sorted(_PATHS))
def test_failed_registration_stores_no_exception_text(family, monkeypatch):
    table, _ = _PATHS[family](monkeypatch)
    assert table.items, "registration should still persist a row"
    row = next(iter(table.items.values()))
    _assert_clean(json.dumps(row, default=str), f"{family} registry row")


@pytest.mark.parametrize("family", sorted(_PATHS))
def test_failed_registration_response_is_clean(family, monkeypatch):
    _, resp = _PATHS[family](monkeypatch)
    _assert_clean(resp["body"], f"{family} register response")


@pytest.mark.parametrize("family", sorted(_PATHS))
def test_get_projection_carries_no_exception_text(family, monkeypatch):
    """The blast radius: GET /api/clusters is what every viewer reads."""
    table, _ = _PATHS[family](monkeypatch)
    monkeypatch.setattr(handler.tenancy, "visible_cluster_ids", lambda *_a, **_k: None)
    listed = handler._handle_list(table, {})
    _assert_clean(listed["body"], f"{family} GET /api/clusters")


@pytest.mark.parametrize("family", sorted(_PATHS))
def test_connection_status_semantics_unchanged(family, monkeypatch):
    """Message-content change only: the failure verdict and the 207 stay put."""
    table, resp = _PATHS[family](monkeypatch)
    row = next(iter(table.items.values()))
    assert row["connection_status"] == "failed"
    assert resp["statusCode"] == 207
    assert json.loads(resp["body"])["status"] == "registered_with_warning"


@pytest.mark.parametrize("family", sorted(_PATHS))
def test_stored_reason_is_actionable(family, monkeypatch):
    """Not merely scrubbed: the DBA still learns it was a permissions problem.
    The AccessDenied code drives the static Korean reason the UI shows."""
    table, _ = _PATHS[family](monkeypatch)
    row = next(iter(table.items.values()))
    assert "권한" in row["connection_error"], row["connection_error"]


def test_conn_error_falls_back_to_exception_class_not_message():
    """A non-botocore exception has no error code. The fallback must be the
    CLASS NAME, never the message."""
    reason = handler._conn_error(RuntimeError(SECRET_FAULT), "unit")
    _assert_clean(reason, "_conn_error fallback")
    assert "RuntimeError" in reason


def test_test_connection_steps_carry_no_exception_text(monkeypatch):
    """POST /api/clusters/test-connection is VIEWER-reachable (non-persisting),
    so its per-step errors were a direct viewer-facing leak."""
    monkeypatch.setattr(
        handler, "_session_for",
        lambda *_a, **_k: MagicMock(
            client=lambda *_a, **_k: MagicMock(
                describe_db_clusters=MagicMock(side_effect=_client_error()))),
    )
    resp = handler._test_connection({"cluster_id": "prod-pg", "region": "ap-northeast-2"})
    _assert_clean(resp["body"], "test-connection steps")
    assert json.loads(resp["body"])["ok"] is False


def test_test_connection_assume_role_step_is_clean(monkeypatch):
    def _boom(*_a, **_k):
        raise _client_error()

    monkeypatch.setattr(handler, "_session_for", _boom)
    resp = handler._test_connection({
        "cluster_id": "prod-pg", "region": "ap-northeast-2",
        "spoke_role_arn": f"arn:aws:iam::{HUB_ACCOUNT}:role/{PLATFORM_ROLE}",
    })
    # The caller SUPPLIED the role arn, so echoing their own input back is not a
    # leak; the exception text is. Check the step error field specifically.
    step = json.loads(resp["body"])["steps"][0]
    assert step["status"] == "failed"
    _assert_clean(step["error"], "assume_role step error")


def test_discover_region_errors_are_clean(monkeypatch):
    monkeypatch.setattr(handler, "_list_clusters_in_region", lambda *_a, **_k: (_ for _ in ()).throw(_client_error()))
    monkeypatch.setattr(handler, "_scan_all", lambda *_a, **_k: [])
    resp = handler._handle_discover(_FakeTable(), {"region": "ap-northeast-2"})
    _assert_clean(resp["body"], "discover errors_by_region")


def test_bulk_register_failures_are_clean(monkeypatch):
    """The bulk path wraps _handle_register, so it must not re-leak."""
    table = _FakeTable()

    def _boom(*_a, **_k):
        raise _client_error()

    monkeypatch.setattr(handler, "_handle_register", _boom)
    resp = handler._handle_bulk_register(table, {"clusters": [{"cluster_id": "prod-pg"}]})
    _assert_clean(resp["body"], "bulk register failed[]")
    assert json.loads(resp["body"])["counts"]["failed"] == 1
