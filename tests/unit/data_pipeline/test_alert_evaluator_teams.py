"""Tests for Teams MessageCard delivery in alert_evaluator/handler.py.

Covers:
  - _build_teams_payload shape (MessageCard, themeColor, 4 facts)
  - potentialAction present when FRONTEND_URL set / absent when unset
  - delivery loop calls _post_json with the teams endpoint for a teams-webhook subscriber
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "data-pipeline" / "alert_evaluator" / "handler.py"


def _load(module_name="alert_evaluator_teams"):
    spec = importlib.util.spec_from_file_location(module_name, HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_RULE = {
    "id": 42,
    "cluster_id": "prod-pg",
    "name": "High CPU",
    "metric_type": "cpu",
    "comparison": ">",
    "threshold": 80.0,
}


def test_teams_payload_type_and_context(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://example.test")
    h = _load("alert_evaluator_teams_a")
    card = h._build_teams_payload(_RULE, 92.4)
    assert card["@type"] == "MessageCard"
    assert card["@context"] == "http://schema.org/extensions"


def test_teams_payload_theme_color_present(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    h = _load("alert_evaluator_teams_b")
    card = h._build_teams_payload(_RULE, 92.4)
    assert "themeColor" in card
    assert len(card["themeColor"]) == 6  # hex without #


def test_teams_payload_four_facts(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    h = _load("alert_evaluator_teams_c")
    card = h._build_teams_payload(_RULE, 92.4)
    facts = card["sections"][0]["facts"]
    fact_names = [f["name"] for f in facts]
    assert fact_names == ["Rule", "Metric", "Threshold", "Observed"]
    # Observed value formatted to 2 decimal places
    observed_fact = next(f for f in facts if f["name"] == "Observed")
    assert "92.40" in observed_fact["value"]


def test_teams_payload_potential_action_when_frontend_set(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://example.test")
    h = _load("alert_evaluator_teams_d")
    card = h._build_teams_payload(_RULE, 92.4)
    assert "potentialAction" in card
    action_names = [a["name"] for a in card["potentialAction"]]
    assert "Open dashboard" in action_names
    assert "Open timeline" in action_names
    assert "Open alerts" in action_names


def test_teams_payload_no_potential_action_without_frontend(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    h = _load("alert_evaluator_teams_e")
    card = h._build_teams_payload(_RULE, 92.4)
    assert "potentialAction" not in card


def test_teams_delivery_loop_posts_to_endpoint(monkeypatch):
    """The _fanout_managed loop must call _post_json(endpoint, ...) for a
    teams-webhook subscriber row."""
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    h = _load("alert_evaluator_teams_f")

    teams_endpoint = "https://x.webhook.office.com/webhook/abc"
    subs = [{"id": 1, "protocol": "teams-webhook", "endpoint": teams_endpoint}]

    def fake_query(sql, params=None):
        if "alert_subscribers_managed" in sql:
            return subs
        return []  # UPDATE calls

    posted_calls = []

    def fake_post(url, payload, timeout=5):
        posted_calls.append((url, payload))
        return 200, "ok"

    with patch.object(h, "_post_json", side_effect=fake_post):
        h._fanout_managed(fake_query, _RULE, 92.4)

    assert len(posted_calls) == 1
    assert posted_calls[0][0] == teams_endpoint
    card = posted_calls[0][1]
    assert card["@type"] == "MessageCard"


def test_slack_delivery_loop_unchanged_for_slack_row(monkeypatch):
    """Slack-webhook rows must still receive Slack Block Kit payloads —
    adding teams-webhook must not break the existing slack path."""
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    h = _load("alert_evaluator_teams_g")

    slack_endpoint = "https://hooks.slack.com/services/T/B/x"
    subs = [{"id": 2, "protocol": "slack-webhook", "endpoint": slack_endpoint}]

    def fake_query(sql, params=None):
        if "alert_subscribers_managed" in sql:
            return subs
        return []

    posted_calls = []

    def fake_post(url, payload, timeout=5):
        posted_calls.append((url, payload))
        return 200, "ok"

    with patch.object(h, "_post_json", side_effect=fake_post):
        h._fanout_managed(fake_query, _RULE, 92.4)

    assert len(posted_calls) == 1
    assert posted_calls[0][0] == slack_endpoint
    # Slack payload uses "blocks" key, not "@type"
    assert "blocks" in posted_calls[0][1]
    assert "@type" not in posted_calls[0][1]
