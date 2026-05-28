"""Tests for /api/slack/command — subcommand parsing + signature gate.

The signature math is identical to slack_interactive (already
covered) so we mostly test that the dispatcher routes to the right
subcommand and that unknown / empty input produces the help block.
"""

import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_PATH = Path(__file__).resolve().parents[3] / "api" / "slack_command" / "handler.py"
_spec = importlib.util.spec_from_file_location("slack_command_handler", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _signed_event(body: dict, secret: str = "test-secret") -> dict:
    raw = "&".join(f"{k}={v}" for k, v in body.items())
    ts = str(int(__import__("time").time()))
    base = f"v0:{ts}:{raw}"
    sig = (
        "v0="
        + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    )
    return {
        "headers": {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
        },
        "body": raw,
        "isBase64Encoded": False,
    }


@patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "test-secret"})
def test_help_when_text_empty():
    event = _signed_event({"text": "", "user_name": "alice"})
    result = handler.lambda_handler(event, None)
    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert any(
        "DBOps shortcuts" in (b.get("text", {}).get("text") or "")
        for b in body["blocks"]
    )


@patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "test-secret"})
def test_help_command_keyword():
    event = _signed_event({"text": "help", "user_name": "alice"})
    result = handler.lambda_handler(event, None)
    body = json.loads(result["body"])
    assert "blocks" in body


@patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "test-secret"})
def test_status_requires_cluster_arg():
    event = _signed_event({"text": "status", "user_name": "alice"})
    result = handler.lambda_handler(event, None)
    body = json.loads(result["body"])
    assert "Usage" in body["text"]


@patch.dict(
    "os.environ",
    {"SLACK_SIGNING_SECRET": "test-secret", "CLUSTERS_TABLE": "clusters"},
)
@patch.object(handler, "boto3")
def test_status_found(mock_boto3):
    mock_table = MagicMock()
    mock_table.get_item.return_value = {
        "Item": {
            "cluster_id": "prod-pg-1",
            "engine": "aurora-postgresql",
            "region": "ap-northeast-2",
            "connection_status": "ok",
        }
    }
    mock_boto3.resource.return_value.Table.return_value = mock_table

    event = _signed_event(
        {"text": "status prod-pg-1", "user_name": "alice"},
    )
    result = handler.lambda_handler(event, None)
    body = json.loads(result["body"])
    text = body["blocks"][0]["text"]["text"]
    assert "prod-pg-1" in text
    assert "🟢" in text  # ok status


@patch.dict(
    "os.environ",
    {"SLACK_SIGNING_SECRET": "test-secret", "CLUSTERS_TABLE": "clusters"},
)
@patch.object(handler, "boto3")
def test_status_unknown_cluster(mock_boto3):
    mock_table = MagicMock()
    mock_table.get_item.return_value = {}
    mock_boto3.resource.return_value.Table.return_value = mock_table

    event = _signed_event(
        {"text": "status no-such-cluster", "user_name": "alice"},
    )
    result = handler.lambda_handler(event, None)
    assert "not registered" in json.loads(result["body"])["text"]


@patch.dict(
    "os.environ",
    {"SLACK_SIGNING_SECRET": "test-secret", "FRONTEND_URL": "https://x.cloudfront.net"},
)
def test_timeline_returns_url():
    event = _signed_event(
        {"text": "timeline prod-pg-1", "user_name": "alice"},
    )
    result = handler.lambda_handler(event, None)
    body = json.loads(result["body"])
    actions = next(b for b in body["blocks"] if b["type"] == "actions")
    url = actions["elements"][0]["url"]
    assert url == "https://x.cloudfront.net/timeline?cluster=prod-pg-1"


@patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "test-secret"})
def test_unknown_subcommand():
    event = _signed_event({"text": "wat", "user_name": "alice"})
    result = handler.lambda_handler(event, None)
    assert "Unknown subcommand" in json.loads(result["body"])["text"]


@patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "test-secret"})
def test_bad_signature_rejected():
    event = {
        "headers": {
            "x-slack-request-timestamp": str(int(__import__("time").time())),
            "x-slack-signature": "v0=deadbeef",
        },
        "body": "text=status",
        "isBase64Encoded": False,
    }
    result = handler.lambda_handler(event, None)
    # Returns 200 with error text (so Slack doesn't retry), but the
    # text starts with the failure marker.
    assert "verification failed" in json.loads(result["body"])["text"]


@patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "test-secret"})
def test_stale_request_rejected():
    """Replay older than 5min should refuse."""
    ts = str(int(__import__("time").time()) - 60 * 10)  # 10min ago
    base = f"v0:{ts}:text=status"
    sig = (
        "v0="
        + hmac.new(b"test-secret", base.encode(), hashlib.sha256).hexdigest()
    )
    event = {
        "headers": {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
        },
        "body": "text=status",
        "isBase64Encoded": False,
    }
    result = handler.lambda_handler(event, None)
    assert "stale" in json.loads(result["body"])["text"]
