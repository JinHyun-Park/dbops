"""Tests for the chat sessions API.

Covers: auth gating, list-by-user, get/put/delete ownership checks,
message normalization + truncation, the 401 unauth path, and the
"someone else's session" 404/403 gates.
"""

import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "api" / "chat_sessions"))

import handler  # noqa: E402


def _jwt(sub: str = "user-a") -> str:
    """Minimal unsigned JWT with sub claim — handler decodes payload only."""
    payload = json.dumps({"sub": sub}).encode("utf-8")
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, path="", body=None, path_params=None, qs=None, sub="user-a"):
    e = {
        "httpMethod": method,
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "pathParameters": path_params or {},
        "queryStringParameters": qs or {},
        "headers": {"authorization": f"Bearer {_jwt(sub)}"},
    }
    if body is not None:
        e["body"] = json.dumps(body) if not isinstance(body, str) else body
    return e


def test_no_auth_returns_401():
    e = _event("GET")
    e["headers"] = {}
    res = handler.lambda_handler(e, None)
    assert res["statusCode"] == 401


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_list_queries_by_user_id(mock_boto3):
    table = MagicMock()
    table.query.return_value = {"Items": [{"session_id": "s1", "title": "Demo"}]}
    mock_boto3.resource.return_value.Table.return_value = table

    res = handler.lambda_handler(_event("GET"), None)

    assert res["statusCode"] == 200
    payload = json.loads(res["body"])
    assert len(payload["sessions"]) == 1
    # Query must use the user-updated index and the user's sub.
    call = table.query.call_args.kwargs
    assert call["IndexName"] == "user-updated-index"
    assert call["ScanIndexForward"] is False  # newest first


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_get_session_owner_can_read(mock_boto3):
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {
            "session_id": "s1",
            "user_id": "user-a",
            "title": "T",
            "messages": [{"role": "user", "content": "hi"}],
        }
    }
    mock_boto3.resource.return_value.Table.return_value = table

    res = handler.lambda_handler(
        _event("GET", path_params={"id": "s1"}), None,
    )
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["session_id"] == "s1"


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_get_session_other_user_404s(mock_boto3):
    """Cross-user reads must look like 'not found', not 403 — don't leak existence."""
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {"session_id": "s1", "user_id": "user-b", "title": "Other"}
    }
    mock_boto3.resource.return_value.Table.return_value = table

    res = handler.lambda_handler(
        _event("GET", path_params={"id": "s1"}, sub="user-a"), None,
    )
    assert res["statusCode"] == 404


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_put_new_session(mock_boto3):
    table = MagicMock()
    table.get_item.return_value = {}  # no existing row
    mock_boto3.resource.return_value.Table.return_value = table

    body = {
        "title": "My investigation",
        "cluster_id": "prod-pg-1",
        "messages": [
            {"role": "user", "content": "what's wrong"},
            {"role": "assistant", "content": "cpu spiked"},
        ],
    }
    res = handler.lambda_handler(
        _event("PUT", path_params={"id": "sess-1"}, body=body), None,
    )

    assert res["statusCode"] == 200
    put_item = table.put_item.call_args.kwargs["Item"]
    assert put_item["session_id"] == "sess-1"
    assert put_item["user_id"] == "user-a"
    assert put_item["title"] == "My investigation"
    assert put_item["message_count"] == 2
    # TTL must be set so orphan sessions auto-expire.
    assert "ttl" in put_item


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_put_someone_elses_session_is_forbidden(mock_boto3):
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {"session_id": "s1", "user_id": "user-b"}
    }
    mock_boto3.resource.return_value.Table.return_value = table

    res = handler.lambda_handler(
        _event("PUT", path_params={"id": "s1"}, body={"title": "hijack"}), None,
    )
    assert res["statusCode"] == 403
    table.put_item.assert_not_called()


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_put_preserves_created_at(mock_boto3):
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {"session_id": "s1", "user_id": "user-a", "created_at": 1_000_000}
    }
    mock_boto3.resource.return_value.Table.return_value = table

    res = handler.lambda_handler(
        _event("PUT", path_params={"id": "s1"}, body={"title": "x"}), None,
    )
    assert res["statusCode"] == 200
    put_item = table.put_item.call_args.kwargs["Item"]
    assert put_item["created_at"] == 1_000_000
    # updated_at should be newer than created_at on subsequent writes.
    assert put_item["updated_at"] != 1_000_000


def test_normalize_messages_truncates_long_list():
    msgs = [{"role": "user", "content": f"msg-{i}"} for i in range(handler.MAX_EMBEDDED_MESSAGES + 50)]
    out = handler._normalize_messages(msgs)
    assert len(out) == handler.MAX_EMBEDDED_MESSAGES
    # Most recent must be preserved.
    assert out[-1]["content"].endswith(str(handler.MAX_EMBEDDED_MESSAGES + 49))


def test_normalize_messages_strips_unexpected_fields():
    msgs = [{"role": "user", "content": "hi", "evil": "<script>"}]
    out = handler._normalize_messages(msgs)
    assert out[0] == {"role": "user", "content": "hi", "tool_calls": [], "ts": out[0]["ts"]}
    assert "evil" not in out[0]


def test_normalize_messages_handles_tool_calls_alias():
    """Frontend sends `toolCalls` (camelCase); handler accepts both."""
    msgs = [{"role": "assistant", "content": "ok", "toolCalls": [{"name": "execute_sql"}]}]
    out = handler._normalize_messages(msgs)
    assert out[0]["tool_calls"] == [{"name": "execute_sql"}]


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_delete_only_owner(mock_boto3):
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {"session_id": "s1", "user_id": "user-b"}
    }
    mock_boto3.resource.return_value.Table.return_value = table

    res = handler.lambda_handler(
        _event("DELETE", path_params={"id": "s1"}, sub="user-a"), None,
    )
    assert res["statusCode"] == 403
    table.delete_item.assert_not_called()


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_delete_owner_succeeds(mock_boto3):
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {"session_id": "s1", "user_id": "user-a"}
    }
    mock_boto3.resource.return_value.Table.return_value = table

    res = handler.lambda_handler(
        _event("DELETE", path_params={"id": "s1"}, sub="user-a"), None,
    )
    assert res["statusCode"] == 200
    table.delete_item.assert_called_once_with(Key={"session_id": "s1"})
