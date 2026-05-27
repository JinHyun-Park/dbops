"""Unit tests for the Slack interactive ack endpoint.

The HMAC verification is the only authentication on this Lambda — if it
fails open, any caller can mutate `alert_rules.last_acked_*` and inject
fake event_log audit rows. These tests are the safety net for that path.

We mock `_rds_data` so the tests stay offline and never hit RDS.
"""

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import time
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "api" / "slack_interactive" / "handler.py"

SECRET = "testsecret_abc"


def _load_handler(monkeypatch):
    """Load slack_interactive/handler.py as a uniquely-named module so it
    doesn't collide with the other handler.py files in the test suite."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:test:cluster")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:test:secret")
    spec = importlib.util.spec_from_file_location(
        "slack_interactive_handler", HANDLER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["slack_interactive_handler"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _signed_event(
    payload: dict,
    secret: str = SECRET,
    ts: int | None = None,
    *,
    skew_seconds: int = 0,
) -> dict:
    """Build a Lambda event whose body is a urlencoded `payload=<json>` and
    whose headers carry a valid Slack v0 signature."""
    if ts is None:
        ts = int(time.time()) + skew_seconds
    raw_body = "payload=" + urllib.parse.quote(json.dumps(payload), safe="")
    basestring = f"v0:{ts}:{raw_body}"
    sig = (
        "v0="
        + hmac.new(secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    )
    return {
        "requestContext": {"http": {"method": "POST", "path": "/api/slack/interactive"}},
        "rawPath": "/api/slack/interactive",
        "headers": {
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        "body": raw_body,
    }


def _ack_payload(rule_id: int, cluster_id: str, user: str = "alice"):
    return {
        "type": "block_actions",
        "user": {"id": "U1", "username": user, "name": user},
        "team": {"domain": "dbops-test"},
        "channel": {"name": "alerts"},
        "actions": [
            {"action_id": "ack_alert", "value": f"{rule_id}:{cluster_id}"}
        ],
    }


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def test_missing_secret_rejected_with_helpful_message(monkeypatch):
    """When the deployment hasn't been wired up yet (signing secret blank)
    the handler must say so explicitly — silent 500s mask the real cause."""
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:test:cluster")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:test:secret")
    spec = importlib.util.spec_from_file_location(
        "slack_interactive_handler_empty", HANDLER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    event = _signed_event(_ack_payload(67, "prod-pg"))
    resp = module.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert "SLACK_SIGNING_SECRET not configured" in body["text"]


def test_valid_signature_passes_verification(monkeypatch):
    h = _load_handler(monkeypatch)
    event = _signed_event(_ack_payload(67, "prod-pg"))

    with patch.object(h, "_execute") as mock_exec:
        mock_exec.side_effect = [
            [{"id": 67, "name": "cpu rule", "cluster_id": "prod-pg", "acked_at": "2026-05-28 11:00:00+00"}],
            [],
        ]
        resp = h.lambda_handler(event, None)

    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert body["replace_original"] is True
    assert "alice" in body["text"]
    # Both writes happened: UPDATE alert_rules, INSERT event_log
    assert mock_exec.call_count == 2


def test_wrong_signature_rejected(monkeypatch):
    h = _load_handler(monkeypatch)
    event = _signed_event(_ack_payload(67, "prod-pg"), secret="wrong-secret")
    resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert "signature mismatch" in body["text"]


def test_stale_timestamp_rejected(monkeypatch):
    """A signed request from 10 minutes ago must be rejected — that's the
    replay-window defense (Slack docs spec 5 min)."""
    h = _load_handler(monkeypatch)
    event = _signed_event(_ack_payload(67, "prod-pg"), skew_seconds=-600)
    resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert "stale" in body["text"]


def test_future_timestamp_rejected(monkeypatch):
    """Clock skew in the *other* direction must also be rejected — an
    attacker pre-computing far-future signatures shouldn't get a free pass."""
    h = _load_handler(monkeypatch)
    event = _signed_event(_ack_payload(67, "prod-pg"), skew_seconds=600)
    resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert "stale" in body["text"]


def test_missing_signature_headers_rejected(monkeypatch):
    h = _load_handler(monkeypatch)
    event = {
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/api/slack/interactive",
        "headers": {},
        "body": "payload=%7B%7D",
    }
    resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert "missing" in body["text"].lower()


def test_base64_encoded_body_supported(monkeypatch):
    """API Gateway HTTP API can deliver bodies base64-encoded. The handler
    must decode them before computing the HMAC."""
    h = _load_handler(monkeypatch)
    event = _signed_event(_ack_payload(67, "prod-pg"))
    raw = event["body"]
    event["body"] = base64.b64encode(raw.encode()).decode()
    event["isBase64Encoded"] = True

    with patch.object(h, "_execute") as mock_exec:
        mock_exec.side_effect = [
            [{"id": 67, "name": "cpu rule", "cluster_id": "prod-pg", "acked_at": "ts"}],
            [],
        ]
        resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert body["replace_original"] is True


# ---------------------------------------------------------------------------
# Payload routing + value parsing
# ---------------------------------------------------------------------------


def test_unknown_action_id_returns_ephemeral_warning(monkeypatch):
    h = _load_handler(monkeypatch)
    payload = _ack_payload(67, "prod-pg")
    payload["actions"][0]["action_id"] = "delete_rule"
    event = _signed_event(payload)
    resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert "unknown action" in body["text"]


def test_malformed_value_returns_ephemeral_warning(monkeypatch):
    h = _load_handler(monkeypatch)
    payload = _ack_payload(67, "prod-pg")
    payload["actions"][0]["value"] = "not-a-pair"  # no colon split
    event = _signed_event(payload)
    resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert "malformed" in body["text"]


def test_empty_actions_returns_ephemeral_warning(monkeypatch):
    h = _load_handler(monkeypatch)
    payload = _ack_payload(67, "prod-pg")
    payload["actions"] = []
    event = _signed_event(payload)
    resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert "no action" in body["text"]


def test_missing_rule_in_db_returns_404_message(monkeypatch):
    h = _load_handler(monkeypatch)
    event = _signed_event(_ack_payload(9999, "prod-pg"))
    with patch.object(h, "_execute") as mock_exec:
        mock_exec.return_value = []  # UPDATE returned no rows
        resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert "not found" in body["text"]


def test_db_error_returns_ephemeral_without_leaking_details(monkeypatch):
    """An unexpected DB failure should yield a 200 + ephemeral warning to
    the user (so Slack doesn't show "we had trouble"), and the underlying
    exception text must stay in CloudWatch — never in the response body."""
    h = _load_handler(monkeypatch)
    event = _signed_event(_ack_payload(67, "prod-pg"))
    with patch.object(h, "_execute") as mock_exec:
        mock_exec.side_effect = RuntimeError("DB password: hunter2")
        resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200
    assert "could not persist" in body["text"]
    assert "hunter2" not in body["text"]  # secret must not leak


def test_response_blocks_include_replace_original_flag(monkeypatch):
    """The Slack message that fired the button must be REPLACED, not
    appended — replace_original keeps the channel from accumulating
    duplicate alert messages after every ack."""
    h = _load_handler(monkeypatch)
    event = _signed_event(_ack_payload(67, "prod-pg", user="bob"))
    with patch.object(h, "_execute") as mock_exec:
        mock_exec.side_effect = [
            [{"id": 67, "name": "compound rule", "cluster_id": "prod-pg", "acked_at": "2026-05-28 11:00:00"}],
            [],
        ]
        resp = h.lambda_handler(event, None)
    body = json.loads(resp["body"])
    assert body["replace_original"] is True
    # Three blocks: header / section / context
    assert len(body["blocks"]) == 3
    assert "bob" in body["blocks"][1]["text"]["text"]
