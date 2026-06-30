"""Tests for the chat sessions API.

Covers: auth gating, list-by-user, get/put/delete ownership checks,
message normalization + truncation, the 401 unauth path, and the
"someone else's session" 404/403 gates.
"""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

# Load the Lambda handler under a unique module name. Several Lambdas in
# this project ship as `handler.py`; a plain sys.path.insert + import
# handler collides with siblings depending on test ordering.
_HANDLER_PATH = (
    Path(__file__).resolve().parents[3] / "api" / "chat_sessions" / "handler.py"
)
_spec = importlib.util.spec_from_file_location("chat_sessions_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


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
    # ProjectionExpression must include last_error so the sidebar badge works.
    assert "last_error" in call["ProjectionExpression"]


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_list_includes_last_error_when_present(mock_boto3):
    """When a stored session carries last_error the LIST response returns it."""
    error_payload = {"message": "stream timeout", "at": 1700000000000}
    table = MagicMock()
    table.query.return_value = {
        "Items": [
            {
                "session_id": "s-err",
                "title": "Broken session",
                "last_error": error_payload,
            }
        ]
    }
    mock_boto3.resource.return_value.Table.return_value = table

    res = handler.lambda_handler(_event("GET"), None)

    assert res["statusCode"] == 200
    sessions = json.loads(res["body"])["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["last_error"] == error_payload


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_list_session_without_last_error_is_unchanged(mock_boto3):
    """Sessions without last_error still list fine — field is simply absent."""
    table = MagicMock()
    table.query.return_value = {
        "Items": [{"session_id": "s-ok", "title": "Healthy session"}]
    }
    mock_boto3.resource.return_value.Table.return_value = table

    res = handler.lambda_handler(_event("GET"), None)

    assert res["statusCode"] == 200
    sessions = json.loads(res["body"])["sessions"]
    assert len(sessions) == 1
    assert "last_error" not in sessions[0]


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


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_put_persists_token_fields(mock_boto3):
    """PUT body with all four token/error fields → stored item carries them."""
    table = MagicMock()
    table.get_item.return_value = {}  # no existing row
    mock_boto3.resource.return_value.Table.return_value = table

    body = {
        "title": "token test",
        "cluster_id": "prod-pg-1",
        "messages": [],
        "total_input_tokens": 1234,
        "total_output_tokens": 567,
        "turn_count": 3,
        "last_error": {"message": "stream timeout", "at": 1700000000000},
    }
    res = handler.lambda_handler(
        _event("PUT", path_params={"id": "sess-tok"}, body=body), None,
    )

    assert res["statusCode"] == 200
    put_item = table.put_item.call_args.kwargs["Item"]
    assert put_item["total_input_tokens"] == 1234
    assert put_item["total_output_tokens"] == 567
    assert put_item["turn_count"] == 3
    assert put_item["last_error"] == {"message": "stream timeout", "at": 1700000000000}


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_put_without_token_fields_is_unchanged(mock_boto3):
    """PUT without token fields → item has no token keys (backward-compatible)."""
    table = MagicMock()
    table.get_item.return_value = {}  # no existing row
    mock_boto3.resource.return_value.Table.return_value = table

    body = {
        "title": "plain session",
        "cluster_id": "dev-pg-1",
        "messages": [],
    }
    res = handler.lambda_handler(
        _event("PUT", path_params={"id": "sess-plain"}, body=body), None,
    )

    assert res["statusCode"] == 200
    put_item = table.put_item.call_args.kwargs["Item"]
    assert "total_input_tokens" not in put_item
    assert "total_output_tokens" not in put_item
    assert "turn_count" not in put_item
    assert "last_error" not in put_item


def test_normalize_messages_persists_followups_and_incomplete():
    """followups (capped) and incomplete (bool-coerced) survive a round-trip."""
    msgs = [
        {
            "role": "assistant",
            "content": "ok",
            "followups": ["q1", "q2", "q3", "q4", "q5", "q6"],  # 6 → capped to 5
            "incomplete": True,
        },
        {
            "role": "assistant",
            "content": "done",
            # No followups, no incomplete — must not appear in output.
        },
    ]
    out = handler._normalize_messages(msgs)
    # followups capped at 5
    assert out[0]["followups"] == ["q1", "q2", "q3", "q4", "q5"]
    assert out[0]["incomplete"] is True
    # Clean message has neither field
    assert "followups" not in out[1]
    assert "incomplete" not in out[1]


def test_normalize_messages_followup_length_capped():
    """Each individual followup string is capped at 300 chars."""
    long_q = "x" * 400
    msgs = [{"role": "assistant", "content": "ok", "followups": [long_q]}]
    out = handler._normalize_messages(msgs)
    assert len(out[0]["followups"][0]) == 300


def test_normalize_messages_incomplete_false_not_stored():
    """incomplete=False (or falsy) must not appear in output."""
    msgs = [{"role": "assistant", "content": "ok", "incomplete": False}]
    out = handler._normalize_messages(msgs)
    assert "incomplete" not in out[0]


def test_normalize_messages_followups_drops_non_string():
    """Non-string followup items (dict, int, None) must be DROPPED, not repr-coerced."""
    msgs = [
        {
            "role": "assistant",
            "content": "ok",
            "followups": [
                "valid string",
                {"text": "a dict"},   # must be dropped
                42,                   # must be dropped
                None,                 # must be dropped
                "another valid",
            ],
        }
    ]
    out = handler._normalize_messages(msgs)
    followups = out[0]["followups"]
    assert followups == ["valid string", "another valid"], (
        f"Expected only string items, got {followups}"
    )


@patch.dict("os.environ", {"SESSIONS_TABLE": "sessions"})
@patch.object(handler, "boto3")
def test_put_rejects_non_int_token_fields(mock_boto3):
    """String and bool values for token fields must NOT be stored — the additive
    guard drops them silently rather than storing invalid types in DDB."""
    table = MagicMock()
    table.get_item.return_value = {}  # no existing row
    mock_boto3.resource.return_value.Table.return_value = table

    body = {
        "title": "bad types",
        "cluster_id": "dev-pg-1",
        "messages": [],
        # string — must be rejected
        "total_input_tokens": "bad",
        # bool — subclasses int in Python, must still be rejected
        "total_output_tokens": True,
        # also bool
        "turn_count": False,
    }
    res = handler.lambda_handler(
        _event("PUT", path_params={"id": "sess-badtype"}, body=body), None,
    )

    assert res["statusCode"] == 200
    put_item = table.put_item.call_args.kwargs["Item"]
    assert "total_input_tokens" not in put_item, "string must not be stored"
    assert "total_output_tokens" not in put_item, "True (bool) must not be stored"
    assert "turn_count" not in put_item, "False (bool) must not be stored"
