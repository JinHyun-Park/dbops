"""Regression: the approval + backup write paths must not echo AWS exception
text into their responses.

These three handlers are the sharpest end of the leak class the tree-wide guard
(tests/unit/mcp_servers/test_handler_error_leaks.py) scans for:

  * api/approvals `_execute_enable_data_api` runs under the DBA's approve click
    and its `error` string is returned in the PUT body AND persisted to the row's
    `execution_error`, which the Approval Center reads back.
  * api/backups drives snapshot creation and restore; its 502 body renders in
    the browser, and an RDS fault spells out the cluster ARN, the hub account id
    and the platform role.
  * api/config PUT returns a per-key validation reason.

The guard is a SOURCE scan; this file is the behavioural half: inject a fault
carrying a fake hub account / role / ARN and assert none of it reaches the body,
while the status codes and ok/verdict fields stay exactly as they were.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

_API = Path(__file__).resolve().parents[3] / "api"
sys.path.insert(0, str(_API / "approvals"))  # api/approvals/handler.py does `import tenancy`
os.environ.setdefault("APPROVALS_TABLE", "approvals-stub")
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


approvals = _load("approvals_handler_leak", _API / "approvals" / "handler.py")
backups = _load("backups_handler_leak", _API / "backups" / "handler.py")
config = _load("config_handler_leak", _API / "config" / "handler.py")


HUB_ACCOUNT = "999988887777"
PLATFORM_ROLE = "dbops-prod-api-backups-role"
TARGET_ARN = f"arn:aws:rds:ap-northeast-2:{HUB_ACCOUNT}:cluster:super-secret-prod"
SECRET_FAULT = (
    f"User: arn:aws:sts::{HUB_ACCOUNT}:assumed-role/{PLATFORM_ROLE}/session is not "
    f"authorized to perform: rds:CreateDBClusterSnapshot on resource: {TARGET_ARN}"
)


def _client_error(op="CreateDBClusterSnapshot"):
    return ClientError({"Error": {"Code": "AccessDenied", "Message": SECRET_FAULT}}, op)


def _assert_clean(blob: str, where: str):
    assert SECRET_FAULT not in blob, f"raw fault leaked into {where}"
    assert HUB_ACCOUNT not in blob, f"hub account id leaked into {where}"
    assert PLATFORM_ROLE not in blob, f"platform role name leaked into {where}"
    assert TARGET_ARN not in blob, f"target ARN leaked into {where}"
    assert "assumed-role" not in blob, f"role session leaked into {where}"
    assert "not authorized to perform" not in blob, f"fault text leaked into {where}"


def _rds_mock(**side_effects):
    """An rds client double whose `.exceptions.<Fault>` are REAL classes.

    A bare MagicMock here makes `except rds.exceptions.SomeFault:` raise
    TypeError (catching a non-class), which would mask the branch under test.
    """
    rds = MagicMock(**{k: MagicMock(side_effect=v) for k, v in side_effects.items()})
    rds.exceptions.DBClusterSnapshotAlreadyExistsFault = type(
        "DBClusterSnapshotAlreadyExistsFault", (Exception,), {})
    rds.exceptions.DBClusterAlreadyExistsFault = type(
        "DBClusterAlreadyExistsFault", (Exception,), {})
    return rds


# --- api/backups ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    """The audit row is the sanctioned home for the raw detail (server-side
    trail); it needs the Data API, so stub it. These tests are about the body."""
    monkeypatch.setattr(backups, "_audit", lambda *a, **k: None)


def test_snapshot_failure_body_carries_no_exception_text(monkeypatch):
    monkeypatch.setattr(
        backups, "_rds_for_cluster",
        lambda *_a, **_k: _rds_mock(create_db_cluster_snapshot=_client_error()),
    )
    resp = backups._handle_snapshot("prod-pg", "dba", {"snapshot_id": "manual-snap-1"})
    _assert_clean(resp["body"], "POST /snapshot 502 body")
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 502
    assert body["error"] == "create_snapshot failed"
    # Still actionable: the caller's own snapshot id + the bounded AWS code.
    assert "manual-snap-1" in body["message"]
    assert "AccessDenied" in body["message"]


def test_restore_failure_body_carries_no_exception_text(monkeypatch):
    monkeypatch.setattr(backups, "_lookup_cluster",
                        lambda *_a, **_k: {"region": "ap-northeast-2", "spoke_role_arn": ""})
    rds = _rds_mock(restore_db_cluster_from_snapshot=_client_error("RestoreDBClusterFromSnapshot"))
    monkeypatch.setattr(backups, "_session_for",
                        lambda *_a, **_k: MagicMock(client=lambda *_a, **_k: rds))
    monkeypatch.setattr(backups, "_source_restore_kwargs",
                        lambda *_a, **_k: ({}, "aurora-postgresql"))
    resp = backups._handle_restore("prod-pg", "dba", {
        "new_cluster_id": "restored-pg", "confirm": "restored-pg",
        "mode": "snapshot", "snapshot_id": "snap-1",
    })
    _assert_clean(resp["body"], "POST /restore 502 body")
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 502
    assert body["error"] == "restore failed"
    assert "restored-pg" in body["message"] and "AccessDenied" in body["message"]


def test_non_botocore_failure_has_no_code_and_still_no_text(monkeypatch):
    """A driver/runtime error has no AWS code. The fallback must be static text,
    never the exception message."""
    monkeypatch.setattr(
        backups, "_rds_for_cluster",
        lambda *_a, **_k: _rds_mock(create_db_cluster_snapshot=RuntimeError(SECRET_FAULT)),
    )
    resp = backups._handle_snapshot("prod-pg", "dba", {})
    _assert_clean(resp["body"], "POST /snapshot 502 body (non-botocore)")
    assert resp["statusCode"] == 502
    assert json.loads(resp["body"])["error"] == "create_snapshot failed"


# --- api/approvals (approve-time auto-execute) ------------------------------


def _approvals_boto3(get_item=None, rds=None):
    mock = MagicMock()
    table = MagicMock()
    if isinstance(get_item, Exception):
        table.get_item.side_effect = get_item
    else:
        table.get_item.return_value = {"Item": get_item or {}}
    mock.resource.return_value.Table.return_value = table
    mock.client.return_value = rds or MagicMock()
    return mock


def test_enable_data_api_registry_lookup_failure_is_clean(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    monkeypatch.setattr(approvals, "boto3", _approvals_boto3(get_item=_client_error("GetItem")))
    out = approvals._execute_enable_data_api({"cluster_id": "prod-pg"})
    assert out["ok"] is False
    _assert_clean(out["error"], "enable_data_api registry lookup error")
    # Which STEP failed must survive the scrub, plus the bounded AWS code.
    assert "레지스트리" in out["error"] and "AccessDenied" in out["error"]


def test_enable_data_api_call_failure_is_clean(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    rds = MagicMock(enable_http_endpoint=MagicMock(side_effect=_client_error("EnableHttpEndpoint")))
    monkeypatch.setattr(
        approvals, "boto3",
        _approvals_boto3(get_item={"cluster_id": "prod-pg", "cluster_arn": TARGET_ARN}, rds=rds),
    )
    out = approvals._execute_enable_data_api({"cluster_id": "prod-pg"})
    assert out["ok"] is False
    _assert_clean(out["error"], "enable_data_api execution error")
    assert "EnableHttpEndpoint" in out["error"] and "AccessDenied" in out["error"]


# --- api/config -------------------------------------------------------------


def test_config_put_validation_reason_is_static(monkeypatch):
    """The validator's ValueError text is log-only: a future validator that
    echoes the rejected value must not put it in the body."""
    monkeypatch.setitem(
        config.CONFIG_KEYS, "TICKETING_PROVIDER",
        ("none", lambda raw: (_ for _ in ()).throw(ValueError(SECRET_FAULT))),
    )
    monkeypatch.setattr(config, "_table", lambda: MagicMock())
    resp = config.lambda_handler({
        "requestContext": {"http": {"method": "PUT"}},
        "headers": {"authorization": "Bearer hdr.eyJjb2duaXRvOmdyb3VwcyI6WyJkYm9wcy1hZG1pbiJdfQ.sig"},
        "body": json.dumps({"config": {"TICKETING_PROVIDER": "../../etc"}}),
    })
    assert resp["statusCode"] == 400
    _assert_clean(resp["body"], "PUT /api/config 400 body")
    assert "TICKETING_PROVIDER" in json.loads(resp["body"])["error"]
