"""Tests for teams-webhook subscriber protocol in api/alerts/handler.py.

Covers:
  - _MANAGED_PROTOCOLS includes "teams-webhook"
  - _create_subscription with https teams endpoint → 201
  - _create_subscription with non-https teams endpoint → 400
  - slack-webhook and pagerduty-events-v2 validation unchanged
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "api" / "alerts" / "handler.py"


def _load(module_name="api_alerts_handler_teams"):
    spec = importlib.util.spec_from_file_location(module_name, HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


h = _load()


def _make_query(rows=None):
    """Fake query that returns rows on INSERT RETURNING."""
    inserted = rows or [{"id": 99, "protocol": "teams-webhook",
                         "endpoint": "https://x.webhook.office.com/abc",
                         "label": None, "enabled": True}]
    return MagicMock(return_value=inserted)


# ---------------------------------------------------------------------------
# _MANAGED_PROTOCOLS membership
# ---------------------------------------------------------------------------

def test_teams_webhook_in_managed_protocols():
    assert "teams-webhook" in h._MANAGED_PROTOCOLS


def test_slack_still_in_managed_protocols():
    assert "slack-webhook" in h._MANAGED_PROTOCOLS


def test_pagerduty_still_in_managed_protocols():
    assert "pagerduty-events-v2" in h._MANAGED_PROTOCOLS


# ---------------------------------------------------------------------------
# _create_subscription — teams-webhook valid https endpoint → 201
# ---------------------------------------------------------------------------

def test_create_teams_subscription_https_returns_201():
    query = _make_query()
    body = {
        "protocol": "teams-webhook",
        "endpoint": "https://x.webhook.office.com/webhookb2/abc",
    }
    resp = h._create_subscription(None, None, body, query)
    assert resp["statusCode"] == 201
    data = __import__("json").loads(resp["body"])
    assert data["protocol"] == "teams-webhook"
    assert data["managed"] is True


def test_create_teams_subscription_non_https_returns_400():
    query = MagicMock()
    body = {
        "protocol": "teams-webhook",
        "endpoint": "http://x.webhook.office.com/webhookb2/abc",
    }
    resp = h._create_subscription(None, None, body, query)
    assert resp["statusCode"] == 400
    data = __import__("json").loads(resp["body"])
    assert "https" in data["error"].lower()
    query.assert_not_called()


def test_create_teams_subscription_missing_scheme_returns_400():
    query = MagicMock()
    body = {
        "protocol": "teams-webhook",
        "endpoint": "webhook.office.com/abc",
    }
    resp = h._create_subscription(None, None, body, query)
    assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# Existing validations unchanged
# ---------------------------------------------------------------------------

def test_slack_webhook_invalid_endpoint_still_400():
    query = MagicMock()
    body = {
        "protocol": "slack-webhook",
        "endpoint": "https://not-hooks.slack.com/bad",
    }
    resp = h._create_subscription(None, None, body, query)
    assert resp["statusCode"] == 400


def test_pagerduty_short_key_still_400():
    query = MagicMock()
    body = {
        "protocol": "pagerduty-events-v2",
        "endpoint": "shortkey",
    }
    resp = h._create_subscription(None, None, body, query)
    assert resp["statusCode"] == 400
