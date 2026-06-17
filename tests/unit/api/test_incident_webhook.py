"""Inbound incident webhook (P4) — auth, payload parsing, event_log write."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_H = Path(__file__).resolve().parents[3] / "api" / "incident_webhook" / "handler.py"
_spec = importlib.util.spec_from_file_location("incident_webhook_handler", _H)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INCIDENT_WEBHOOK_SECRET", "s3cr3t")
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:cluster")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:secret")
    monkeypatch.setenv("CACHE_DB_NAME", "dbops")


def _evt(body: dict, token: str | None = "s3cr3t") -> dict:
    headers = {"X-DBOps-Webhook-Token": token} if token is not None else {}
    return {"headers": headers, "body": json.dumps(body)}


# --- auth ---

def test_missing_secret_returns_503(monkeypatch):
    monkeypatch.delenv("INCIDENT_WEBHOOK_SECRET", raising=False)
    resp = handler.lambda_handler(_evt({"title": "x"}), None)
    assert resp["statusCode"] == 503


def test_bad_token_returns_401():
    resp = handler.lambda_handler(_evt({"title": "x"}, token="wrong"), None)
    assert resp["statusCode"] == 401


def test_missing_token_returns_401():
    resp = handler.lambda_handler(_evt({"title": "x"}, token=None), None)
    assert resp["statusCode"] == 401


# --- parsing ---

def test_parse_datadog_tags_and_severity():
    inc = handler.parse_incident({
        "alert_type": "error",
        "title": "High CPU",
        "tags": "env:prod,cluster:prod-pg,team:dba",
        "link": "https://app.datadoghq.com/event/1",
    })
    assert inc["source"] == "datadog"
    assert inc["cluster_id"] == "prod-pg"
    assert inc["severity"] == "critical"
    assert inc["url"].startswith("https://app.datadoghq.com")


def test_parse_pagerduty_custom_details():
    inc = handler.parse_incident({
        "event": {
            "data": {
                "title": "DB unreachable",
                "urgency": "high",
                "html_url": "https://acme.pagerduty.com/incidents/Q1",
                "custom_details": {"cluster_id": "prod-mysql"},
            }
        }
    })
    assert inc["source"] == "pagerduty"
    assert inc["cluster_id"] == "prod-mysql"
    assert inc["severity"] == "critical"


def test_parse_generic_fallbacks():
    inc = handler.parse_incident({})
    assert inc["source"] == "external"
    assert inc["title"] == "external incident"
    assert inc["severity"] == "info"
    assert inc["cluster_id"] == ""


# --- end to end: valid request writes one external_incident event_log row ---

def test_valid_request_writes_event_log():
    rds = MagicMock()
    with patch.object(handler.boto3, "client", return_value=rds):
        resp = handler.lambda_handler(
            _evt({
                "alert_type": "warning",
                "title": "Replica lag",
                "tags": ["cluster:prod-pg"],
                "link": "https://x",
            }),
            None,
        )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "recorded"
    assert body["cluster_id"] == "prod-pg"
    assert body["severity"] == "warning"
    # The insert hit event_log with event_type external_incident.
    rds.execute_statement.assert_called_once()
    sql = rds.execute_statement.call_args.kwargs["sql"]
    assert "INSERT INTO event_log" in sql
    assert "external_incident" in sql


def test_invalid_json_returns_400():
    resp = handler.lambda_handler(
        {"headers": {"X-DBOps-Webhook-Token": "s3cr3t"}, "body": "{not json"},
        None,
    )
    assert resp["statusCode"] == 400
