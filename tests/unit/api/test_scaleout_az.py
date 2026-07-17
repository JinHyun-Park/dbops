"""P2-⑥ — POST /api/scaleout-az (AZ scale-out runbook).

The route invokes the READ-ONLY plan_az_scaleout tool, then mints one
add_reader_instance approval (origin="ui") per planned reader. Each approval's
action_details MUST carry a concrete instance_class + availability_zone, and
request_approval is invoked WITHOUT origin (only the trusted API stamps origin
via update_item). Admin + tenant gated (fail-closed). Partial mint failure is a
partial-success shape, never a crash.
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_APPROVALS_DIR = Path(__file__).resolve().parents[3] / "api" / "approvals"
sys.path.insert(0, str(_APPROVALS_DIR))

_PATH = _APPROVALS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("approvals_handler_scaleout_az", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("APPROVALS_TABLE", "approvals-stub")
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    monkeypatch.setenv("TEAM_MEMBERS_TABLE", "team-members-stub")
    monkeypatch.setenv("TEAM_MEMBERS_BY_USER_INDEX", "by-user")
    monkeypatch.setenv("OPERATIONS_FUNCTION_NAME", "dbops-dev-operations-mcp")
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid})


def _jwt(admin=True):
    payload = {
        "cognito:username": "dba-approver",
        "cognito:groups": ["dbops-admin"] if admin else ["dbops-viewer"],
    }
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h.{b64}.s"


def _event(body=None, admin=True, with_token=True, path="/api/scaleout-az", method="POST"):
    headers = {}
    if with_token:
        headers["authorization"] = f"Bearer {_jwt(admin)}"
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "httpMethod": method,
        "rawPath": path,
        "pathParameters": {},
        "queryStringParameters": {},
        "headers": headers,
        "body": json.dumps(body or {}),
    }


def _lambda_resp(tool_result):
    stream = MagicMock()
    stream.read.return_value = json.dumps(
        {"content": [{"type": "text", "text": json.dumps(tool_result)}]}
    ).encode()
    return {"Payload": stream}


def _wire(monkeypatch, plan_result, approval_results):
    """plan_result → returned for the plan_az_scaleout invoke; approval_results →
    a list returned in order, one per request_approval invoke."""
    approvals_table = MagicMock()
    approvals_table.scan.return_value = {"Items": []}
    clusters_table = MagicMock()
    clusters_table.get_item.return_value = {"Item": {"cluster_id": "c1"}}
    mock = MagicMock()

    def _table(name):
        return clusters_table if name == "clusters-stub" else approvals_table

    mock.resource.return_value.Table.side_effect = _table

    lam = MagicMock()
    approval_iter = iter(approval_results)

    def _invoke(**kwargs):
        cc = json.loads(base64.b64decode(kwargs["ClientContext"]))
        tool = cc["custom"]["tool_name"]
        if tool == "plan_az_scaleout":
            return _lambda_resp(plan_result)
        return _lambda_resp(next(approval_iter))

    lam.invoke.side_effect = _invoke
    mock.client.return_value = lam
    monkeypatch.setattr(handler, "boto3", mock)
    return approvals_table, lam


_PLAN_2 = {
    "status": "planned",
    "cluster_id": "c1",
    "exclude_az": "ap-northeast-2b",
    "instance_class": "db.serverless",
    "available_azs": ["ap-northeast-2a", "ap-northeast-2b", "ap-northeast-2c"],
    "healthy_azs": ["ap-northeast-2a", "ap-northeast-2c"],
    "planned_readers": [
        {"new_instance_id": "c1-az1", "availability_zone": "ap-northeast-2a", "instance_class": "db.serverless"},
        {"new_instance_id": "c1-az2", "availability_zone": "ap-northeast-2c", "instance_class": "db.serverless"},
    ],
}


def _request_approval_calls(lam):
    """The invoke calls whose ClientContext tool_name is request_approval."""
    calls = []
    for c in lam.invoke.call_args_list:
        cc = json.loads(base64.b64decode(c.kwargs["ClientContext"]))
        if cc["custom"]["tool_name"] == "request_approval":
            calls.append(c)
    return calls


def test_post_plans_then_mints_add_reader_per_reader(monkeypatch):
    approvals_table, lam = _wire(
        monkeypatch, _PLAN_2,
        [
            {"status": "pending", "approval_id": "a1", "created_at": "1700000000001"},
            {"status": "pending", "approval_id": "a2", "created_at": "1700000000002"},
        ],
    )
    r = handler.lambda_handler(_event(body={"cluster_id": "c1", "exclude_az": "ap-northeast-2b", "count": 2}), None)
    assert r["statusCode"] == 201
    body = json.loads(r["body"])
    assert len(body["created"]) == 2
    assert body["failed"] == []
    assert body["exclude_az"] == "ap-northeast-2b"

    ra = _request_approval_calls(lam)
    assert len(ra) == 2
    for call, reader in zip(ra, _PLAN_2["planned_readers"], strict=False):
        payload = json.loads(call.kwargs["Payload"])
        assert payload["action_type"] == "add_reader_instance"
        assert "origin" not in payload  # the tool never receives origin
        ad = payload["action_details"]
        assert ad["cluster_id"] == "c1"
        assert ad["new_instance_id"] == reader["new_instance_id"]
        assert ad["instance_class"] == "db.serverless"  # concrete class bound
        assert ad["availability_zone"] == reader["availability_zone"]  # concrete AZ bound

    # origin="ui" stamped on each created row via update_item (trusted API only)
    assert approvals_table.update_item.call_count == 2
    for up in approvals_table.update_item.call_args_list:
        assert up.kwargs["ExpressionAttributeValues"][":ui"] == "ui"


def test_post_viewer_forbidden(monkeypatch):
    _, lam = _wire(monkeypatch, _PLAN_2, [])
    r = handler.lambda_handler(_event(body={"cluster_id": "c1", "count": 2}, admin=False), None)
    assert r["statusCode"] == 403
    lam.invoke.assert_not_called()


def test_post_no_token_fail_closed(monkeypatch):
    _, lam = _wire(monkeypatch, _PLAN_2, [])
    r = handler.lambda_handler(_event(body={"cluster_id": "c1", "count": 2}, with_token=False), None)
    assert r["statusCode"] == 403
    lam.invoke.assert_not_called()


def test_post_non_visible_cluster_forbidden(monkeypatch):
    _, lam = _wire(monkeypatch, _PLAN_2, [])
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: False)
    r = handler.lambda_handler(_event(body={"cluster_id": "c-other", "count": 2}), None)
    assert r["statusCode"] == 403
    lam.invoke.assert_not_called()


def test_post_missing_cluster_400(monkeypatch):
    _, lam = _wire(monkeypatch, _PLAN_2, [])
    r = handler.lambda_handler(_event(body={"count": 2}), None)
    assert r["statusCode"] == 400
    lam.invoke.assert_not_called()


def test_post_plan_invalid_az_400_no_mint(monkeypatch):
    plan = {"status": "invalid_az", "cluster_id": "c1",
            "available_azs": ["ap-northeast-2a"], "reason": "제외 AZ가 없습니다."}
    approvals_table, lam = _wire(monkeypatch, plan, [])
    r = handler.lambda_handler(_event(body={"cluster_id": "c1", "exclude_az": "zzz", "count": 2}), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"])["error"] == "plan_failed"
    # only the plan invoke ran — no add_reader_instance approvals minted
    assert _request_approval_calls(lam) == []
    approvals_table.update_item.assert_not_called()


def test_post_partial_mint_failure_partial_success(monkeypatch):
    # first reader mints ok, second request_approval returns a non-pending shape.
    approvals_table, lam = _wire(
        monkeypatch, _PLAN_2,
        [
            {"status": "pending", "approval_id": "a1", "created_at": "1700000000001"},
            {},  # mint failure — no crash, reported under failed[]
        ],
    )
    r = handler.lambda_handler(_event(body={"cluster_id": "c1", "exclude_az": "ap-northeast-2b", "count": 2}), None)
    assert r["statusCode"] == 201  # at least one created → 201
    body = json.loads(r["body"])
    assert len(body["created"]) == 1
    assert len(body["failed"]) == 1
    assert body["failed"][0]["new_instance_id"] == "c1-az2"
    # only the successful mint got an origin stamp
    assert approvals_table.update_item.call_count == 1
