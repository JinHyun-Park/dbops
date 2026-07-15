"""N-① — POST /api/endpoint-requests + approve auto-execute.

POST /api/endpoint-requests invokes the operations Lambda's request_approval
(origin="ui") to mint a payload-hashed approval. On approve, an origin=="ui"
endpoint row is auto-executed by invoking the endpoint tool with approved=true.
A CHAT row (no origin) of the same action_type must NOT auto-execute — the agent
replays those, so auto-executing would double-execute the write.
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
_spec = importlib.util.spec_from_file_location("approvals_handler_endpoints", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("APPROVALS_TABLE", "approvals-stub")
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    monkeypatch.setenv("TEAM_MEMBERS_TABLE", "team-members-stub")
    monkeypatch.setenv("TEAM_MEMBERS_BY_USER_INDEX", "by-user")
    monkeypatch.setenv("OPERATIONS_FUNCTION_NAME", "dbops-dev-operations-mcp")
    # Every test that reaches an invoke/tenant path should be deterministic.
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid})


# --- token / event builders -------------------------------------------------

def _jwt(admin=True):
    payload = {
        "cognito:username": "dba-approver",
        "cognito:groups": ["dbops-admin"] if admin else ["dbops-viewer"],
    }
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h.{b64}.s"


def _event(method="POST", path="/api/endpoint-requests", body=None,
           approval_id=None, admin=True, with_token=True):
    headers = {}
    if with_token:
        headers["authorization"] = f"Bearer {_jwt(admin)}"
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "httpMethod": method,
        "rawPath": path,
        "pathParameters": {"id": approval_id} if approval_id else {},
        "queryStringParameters": {},
        "headers": headers,
        "body": json.dumps(body or {}),
    }


# --- boto3 mock: dynamodb tables + lambda client ----------------------------

def _lambda_resp(tool_result, fn_error=None):
    """The operations Lambda wraps tool results as
    {"content": [{"text": json.dumps(result)}]}; the invoke response Payload is
    a read()-able stream."""
    stream = MagicMock()
    stream.read.return_value = json.dumps(
        {"content": [{"type": "text", "text": json.dumps(tool_result)}]}
    ).encode()
    resp = {"Payload": stream}
    if fn_error:
        resp["FunctionError"] = fn_error
    return resp


def _wire(monkeypatch, approvals_rows=None, tool_result=None, fn_error=None):
    mock = MagicMock()
    approvals_table = MagicMock()
    approvals_table.scan.return_value = {"Items": approvals_rows or []}
    clusters_table = MagicMock()
    clusters_table.get_item.return_value = {"Item": {"cluster_id": "c1"}}

    def _table(name):
        return clusters_table if name == "clusters-stub" else approvals_table

    mock.resource.return_value.Table.side_effect = _table
    lam = MagicMock()
    lam.invoke.return_value = _lambda_resp(tool_result or {}, fn_error)
    mock.client.return_value = lam
    monkeypatch.setattr(handler, "boto3", mock)
    return approvals_table, lam


# ===========================================================================
# POST /api/endpoint-requests
# ===========================================================================

def test_post_create_invokes_request_approval_without_origin_then_stamps(monkeypatch):
    # request_approval is invoked WITHOUT origin (the tool must not accept it);
    # the API stamps origin="ui" onto the created row via update_item.
    approvals_table, lam = _wire(
        monkeypatch,
        tool_result={"status": "pending", "approval_id": "new-1", "created_at": "1700000000000"},
    )
    body = {
        "cluster_id": "c1",
        "action": "create_custom_endpoint",
        "endpoint_identifier": "ep1",
        "endpoint_type": "reader",  # lower-case → normalized to READER
        "static_members": ["i-1", "i-2"],
    }
    r = handler.lambda_handler(_event(body=body), None)
    assert r["statusCode"] == 201
    assert json.loads(r["body"])["approval_id"] == "new-1"

    lam.invoke.assert_called_once()
    call = lam.invoke.call_args
    payload = json.loads(call.kwargs["Payload"])
    assert payload["action_type"] == "create_custom_endpoint"
    assert "origin" not in payload  # the tool never receives origin
    assert payload["action_details"]["endpoint_type"] == "READER"
    assert payload["action_details"]["static_members"] == ["i-1", "i-2"]
    assert payload["action_details"]["excluded_members"] == []
    cc = json.loads(base64.b64decode(call.kwargs["ClientContext"]))
    assert cc["custom"]["tool_name"] == "request_approval"

    # The trusted API Lambda stamps origin="ui" on the (approval_id, created_at)
    # row — this is the ONLY writer of origin, so the agent can't forge it.
    approvals_table.update_item.assert_called_once()
    up = approvals_table.update_item.call_args.kwargs
    assert up["Key"] == {"approval_id": "new-1", "created_at": "1700000000000"}
    assert up["ExpressionAttributeValues"][":ui"] == "ui"


def test_post_delete_maps_minimal_details(monkeypatch):
    _, lam = _wire(monkeypatch, tool_result={"status": "pending", "approval_id": "d-1"})
    body = {"cluster_id": "c1", "action": "delete_custom_endpoint",
            "endpoint_identifier": "ep-old"}
    r = handler.lambda_handler(_event(body=body), None)
    assert r["statusCode"] == 201
    payload = json.loads(lam.invoke.call_args.kwargs["Payload"])
    assert payload["action_type"] == "delete_custom_endpoint"
    assert payload["action_details"] == {"endpoint_identifier": "ep-old"}


def test_post_viewer_forbidden(monkeypatch):
    _, lam = _wire(monkeypatch)
    r = handler.lambda_handler(
        _event(body={"cluster_id": "c1", "action": "delete_custom_endpoint",
                     "endpoint_identifier": "ep1"}, admin=False), None)
    assert r["statusCode"] == 403
    lam.invoke.assert_not_called()


def test_post_no_token_fail_closed(monkeypatch):
    _, lam = _wire(monkeypatch)
    r = handler.lambda_handler(
        _event(body={"cluster_id": "c1", "action": "delete_custom_endpoint",
                     "endpoint_identifier": "ep1"}, with_token=False), None)
    assert r["statusCode"] == 403
    lam.invoke.assert_not_called()


def test_post_non_visible_cluster_forbidden(monkeypatch):
    _, lam = _wire(monkeypatch)
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: False)
    r = handler.lambda_handler(
        _event(body={"cluster_id": "c-other", "action": "delete_custom_endpoint",
                     "endpoint_identifier": "ep1"}), None)
    assert r["statusCode"] == 403
    lam.invoke.assert_not_called()


def test_post_invalid_action_400(monkeypatch):
    _, lam = _wire(monkeypatch)
    r = handler.lambda_handler(
        _event(body={"cluster_id": "c1", "action": "execute_sql",
                     "endpoint_identifier": "ep1"}), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"])["error"] == "invalid_action"
    lam.invoke.assert_not_called()


def test_post_missing_endpoint_identifier_400(monkeypatch):
    _, lam = _wire(monkeypatch)
    r = handler.lambda_handler(
        _event(body={"cluster_id": "c1", "action": "create_custom_endpoint",
                     "endpoint_type": "READER"}), None)
    assert r["statusCode"] == 400
    lam.invoke.assert_not_called()


def test_post_create_bad_endpoint_type_400(monkeypatch):
    _, lam = _wire(monkeypatch)
    r = handler.lambda_handler(
        _event(body={"cluster_id": "c1", "action": "create_custom_endpoint",
                     "endpoint_identifier": "ep1", "endpoint_type": "WRITER"}), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"])["error"] == "invalid_endpoint_type"
    lam.invoke.assert_not_called()


def test_post_modify_requires_members_400(monkeypatch):
    _, lam = _wire(monkeypatch)
    r = handler.lambda_handler(
        _event(body={"cluster_id": "c1", "action": "modify_custom_endpoint",
                     "endpoint_identifier": "ep1"}), None)
    assert r["statusCode"] == 400
    lam.invoke.assert_not_called()


def test_post_mutually_exclusive_members_400(monkeypatch):
    _, lam = _wire(monkeypatch)
    r = handler.lambda_handler(
        _event(body={"cluster_id": "c1", "action": "create_custom_endpoint",
                     "endpoint_identifier": "ep1", "endpoint_type": "READER",
                     "static_members": ["i-1"], "excluded_members": ["i-2"]}), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"])["error"] == "invalid_members"
    lam.invoke.assert_not_called()


def test_post_operations_invoke_failure_502(monkeypatch):
    # request_approval tool didn't return status=pending → friendly 502, no leak.
    _, lam = _wire(monkeypatch, tool_result={}, fn_error="Unhandled")
    r = handler.lambda_handler(
        _event(body={"cluster_id": "c1", "action": "delete_custom_endpoint",
                     "endpoint_identifier": "ep1"}), None)
    assert r["statusCode"] == 502
    assert json.loads(r["body"])["error"] == "request_failed"


# ===========================================================================
# Approve auto-execute (the double-exec gate)
# ===========================================================================

def _endpoint_row(origin=None, action_type="create_custom_endpoint"):
    row = {
        "approval_id": "aid-1",
        "created_at": "2026-07-14T00:00:00",
        "approval_status": "pending",
        "cluster_id": "c1",
        "action_type": action_type,
        "action_details": {"endpoint_identifier": "ep1", "endpoint_type": "READER",
                           "static_members": [], "excluded_members": []},
        "requested_by": "requester",  # differs from approver → no self-approval 403
    }
    if origin:
        row["origin"] = origin
    return row


def test_approve_ui_row_auto_executes(monkeypatch):
    _, lam = _wire(monkeypatch, approvals_rows=[_endpoint_row(origin="ui")],
                   tool_result={"status": "creating", "endpoint_identifier": "ep1"})
    r = handler.lambda_handler(
        _event(method="PUT", path="/api/approvals/aid-1", approval_id="aid-1",
               body={"action": "approve"}), None)
    assert r["statusCode"] == 200
    ex = json.loads(r["body"])["endpoint_execution"]
    assert ex["executed"] is True
    assert ex["status"] == "creating"

    lam.invoke.assert_called_once()
    call = lam.invoke.call_args
    payload = json.loads(call.kwargs["Payload"])
    assert payload["approved"] is True
    assert payload["approval_id"] == "aid-1"
    assert payload["endpoint_identifier"] == "ep1"
    cc = json.loads(base64.b64decode(call.kwargs["ClientContext"]))
    assert cc["custom"]["tool_name"] == "create_custom_endpoint"


def test_approve_chat_row_does_not_execute(monkeypatch):
    """A chat-initiated row (NO origin) of the same action_type must NOT be
    auto-executed — the agent replays it. Double-exec guard."""
    _, lam = _wire(monkeypatch, approvals_rows=[_endpoint_row(origin=None)])
    r = handler.lambda_handler(
        _event(method="PUT", path="/api/approvals/aid-1", approval_id="aid-1",
               body={"action": "approve"}), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["endpoint_execution"] is None
    lam.invoke.assert_not_called()


def test_approve_ui_row_execute_failure_surfaced_no_crash(monkeypatch):
    _, lam = _wire(monkeypatch, approvals_rows=[_endpoint_row(origin="ui")],
                   tool_result={"status": "create_failed", "error": "boom"})
    r = handler.lambda_handler(
        _event(method="PUT", path="/api/approvals/aid-1", approval_id="aid-1",
               body={"action": "approve"}), None)
    assert r["statusCode"] == 502
    body = json.loads(r["body"])
    assert body["status"] == "approved"
    ex = body["endpoint_execution"]
    assert ex["executed"] is False
    assert ex["status"] == "create_failed"
    lam.invoke.assert_called_once()
