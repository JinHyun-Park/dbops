"""Tests for the AgentCore Memory inspector API."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

_PATH = Path(__file__).resolve().parents[3] / "api" / "memory" / "handler.py"
_spec = importlib.util.spec_from_file_location("memory_handler", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _jwt(sub: str = "user-a") -> str:
    payload = json.dumps({"sub": sub}).encode()
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, kind="preferences", record_id=None, sub="user-a"):
    e = {
        "httpMethod": method,
        "requestContext": {"http": {"method": method}},
        "pathParameters": {"id": record_id} if record_id else {},
        "queryStringParameters": {"kind": kind},
        "headers": {"authorization": f"Bearer {_jwt(sub)}"},
    }
    return e


def test_unauth_returns_401():
    e = _event("GET")
    e["headers"] = {}
    res = handler.lambda_handler(e, None)
    assert res["statusCode"] == 401


def test_invalid_kind_400():
    res = handler.lambda_handler(_event("GET", kind="summaries"), None)
    assert res["statusCode"] == 400
    assert "kind must be" in json.loads(res["body"])["error"]


@patch.dict("os.environ", {}, clear=True)
def test_missing_memory_id_env_500():
    res = handler.lambda_handler(_event("GET"), None)
    assert res["statusCode"] == 500
    assert "MEMORY_ID" in json.loads(res["body"])["error"]


@patch.dict("os.environ", {"MEMORY_ID": "mem-123"})
@patch.object(handler, "boto3")
def test_list_records_happy(mock_boto3):
    mock_ac = MagicMock()
    mock_ac.list_memory_records.return_value = {
        "memoryRecordSummaries": [
            {
                "memoryRecordId": "rec-1",
                "content": {"text": "User prefers terse answers"},
                "createdAt": "2026-05-01T00:00:00Z",
                "updatedAt": "2026-05-02T00:00:00Z",
            }
        ]
    }
    mock_boto3.client.return_value = mock_ac

    res = handler.lambda_handler(_event("GET", kind="preferences"), None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["kind"] == "preferences"
    assert body["records"][0]["content"] == "User prefers terse answers"
    # namespace must include the actor (Cognito sub)
    assert "user-a" in body["namespace"]


@patch.dict("os.environ", {"MEMORY_ID": "mem-123"})
@patch.object(handler, "boto3")
def test_list_records_not_found_returns_empty(mock_boto3):
    """ResourceNotFoundException = fresh user with no extracted memories;
    must return empty list rather than 500."""
    mock_ac = MagicMock()
    mock_ac.list_memory_records.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}},
        "ListMemoryRecords",
    )
    mock_boto3.client.return_value = mock_ac

    res = handler.lambda_handler(_event("GET", kind="facts"), None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["records"] == []


@patch.dict("os.environ", {"MEMORY_ID": "mem-123"})
@patch.object(handler, "boto3")
def test_delete_record_happy(mock_boto3):
    mock_ac = MagicMock()
    mock_ac.delete_memory_record.return_value = {}
    mock_boto3.client.return_value = mock_ac

    res = handler.lambda_handler(
        _event("DELETE", kind="preferences", record_id="rec-9"), None,
    )
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["deleted"] == "rec-9"
    mock_ac.delete_memory_record.assert_called_once_with(
        memoryId="mem-123", memoryRecordId="rec-9"
    )


@patch.dict("os.environ", {"MEMORY_ID": "mem-123"})
@patch.object(handler, "boto3")
def test_delete_record_404(mock_boto3):
    mock_ac = MagicMock()
    mock_ac.delete_memory_record.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}},
        "DeleteMemoryRecord",
    )
    mock_boto3.client.return_value = mock_ac

    res = handler.lambda_handler(
        _event("DELETE", kind="preferences", record_id="rec-missing"), None,
    )
    assert res["statusCode"] == 404


@patch.dict("os.environ", {"MEMORY_ID": "mem-123"})
def test_post_method_not_allowed():
    """Memory is read+delete only — no POST/PUT (agent writes are
    handled by AgentCore, not this UI surface)."""
    res = handler.lambda_handler(_event("POST"), None)
    assert res["statusCode"] == 405


@patch.dict("os.environ", {"MEMORY_ID": "mem-123"})
@patch.object(handler, "boto3")
def test_cross_user_namespace_isolation(mock_boto3):
    """sub from JWT must be the actor in the namespace path — user A
    can't peek at user B's preferences just by guessing record IDs."""
    mock_ac = MagicMock()
    mock_ac.list_memory_records.return_value = {"memoryRecordSummaries": []}
    mock_boto3.client.return_value = mock_ac

    handler.lambda_handler(_event("GET", sub="alice"), None)
    call_kwargs = mock_ac.list_memory_records.call_args.kwargs
    assert "alice" in call_kwargs["namespace"]
    assert "bob" not in call_kwargs["namespace"]
