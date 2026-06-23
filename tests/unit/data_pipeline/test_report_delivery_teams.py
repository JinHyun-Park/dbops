"""Tests for Teams MessageCard delivery in report_generator/handler.py.

Covers:
  - _build_report_teams_card shape (MessageCard, themeColor, facts, text)
  - _deliver_report sends teams card to a teams-webhook subscriber
  - _deliver_report sends slack blocks to a slack-webhook subscriber (unchanged)
  - Both protocols handled in a single query pass
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "data-pipeline" / "report_generator" / "handler.py"
HANDLER_DIR = str(HANDLER_PATH.parent)
if HANDLER_DIR not in sys.path:
    sys.path.insert(0, HANDLER_DIR)

_spec = importlib.util.spec_from_file_location("report_generator_handler_teams", HANDLER_PATH)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


# ---------------------------------------------------------------------------
# _build_report_teams_card
# ---------------------------------------------------------------------------

def test_teams_card_type_and_context():
    card = h._build_report_teams_card("prod-pg", "2026-06-23", "daily", "요약 텍스트")
    assert card["@type"] == "MessageCard"
    assert card["@context"] == "http://schema.org/extensions"


def test_teams_card_theme_color():
    card = h._build_report_teams_card("prod-pg", "2026-06-23", "daily", "요약")
    assert card["themeColor"] == "2563EB"


def test_teams_card_facts_present():
    card = h._build_report_teams_card("mycluster", "2026-06-23", "weekly", "요약")
    facts = card["sections"][0]["facts"]
    fact_names = [f["name"] for f in facts]
    assert "클러스터" in fact_names
    assert "유형" in fact_names


def test_teams_card_summary_truncated():
    long_summary = "x" * 3000
    card = h._build_report_teams_card("c1", "2026-06-23", "daily", long_summary)
    assert len(card["sections"][0]["text"]) <= 2800


def test_teams_card_cluster_in_facts():
    card = h._build_report_teams_card("prod-pg", "2026-06-23", "daily", "요약")
    facts = card["sections"][0]["facts"]
    cluster_fact = next(f for f in facts if f["name"] == "클러스터")
    assert "prod-pg" in cluster_fact["value"]


# ---------------------------------------------------------------------------
# _deliver_report with teams-webhook subscriber
# ---------------------------------------------------------------------------

def test_deliver_posts_teams_card_to_teams_sub(monkeypatch):
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:::t")

    teams_endpoint = "https://x.webhook.office.com/webhook/abc"
    cache_query = MagicMock(return_value=[
        {"id": 10, "protocol": "teams-webhook", "endpoint": teams_endpoint}
    ])

    posted_calls = []
    def fake_post(url, payload, timeout=5):
        posted_calls.append((url, payload))
        return 200, "ok"

    with patch.object(h, "boto3"), patch.object(h, "_post_json", side_effect=fake_post):
        h._deliver_report(cache_query, "prod-pg", "2026-06-23", "daily", "요약")

    assert len(posted_calls) == 1
    assert posted_calls[0][0] == teams_endpoint
    card = posted_calls[0][1]
    assert card["@type"] == "MessageCard"


def test_deliver_slack_path_unchanged_for_slack_sub(monkeypatch):
    """Slack-webhook subscribers must still receive Slack Block Kit payloads."""
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:::t")

    slack_endpoint = "https://hooks.slack.com/services/T/B/x"
    cache_query = MagicMock(return_value=[
        {"id": 11, "protocol": "slack-webhook", "endpoint": slack_endpoint}
    ])

    posted_calls = []
    def fake_post(url, payload, timeout=5):
        posted_calls.append((url, payload))
        return 200, "ok"

    with patch.object(h, "boto3"), patch.object(h, "_post_json", side_effect=fake_post):
        h._deliver_report(cache_query, "prod-pg", "2026-06-23", "daily", "요약")

    assert len(posted_calls) == 1
    assert posted_calls[0][0] == slack_endpoint
    payload = posted_calls[0][1]
    assert "blocks" in payload
    assert "@type" not in payload


def test_deliver_both_protocols_in_one_pass(monkeypatch):
    """When both protocols are present both subscribers receive the correct payload."""
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:::t")

    slack_ep = "https://hooks.slack.com/services/T/B/x"
    teams_ep = "https://x.webhook.office.com/webhook/abc"
    cache_query = MagicMock(return_value=[
        {"id": 1, "protocol": "slack-webhook", "endpoint": slack_ep},
        {"id": 2, "protocol": "teams-webhook", "endpoint": teams_ep},
    ])

    posted_calls = []
    def fake_post(url, payload, timeout=5):
        posted_calls.append((url, payload))
        return 200, "ok"

    with patch.object(h, "boto3"), patch.object(h, "_post_json", side_effect=fake_post):
        h._deliver_report(cache_query, "prod-pg", "2026-06-23", "daily", "요약")

    assert len(posted_calls) == 2
    urls = {c[0] for c in posted_calls}
    assert slack_ep in urls
    assert teams_ep in urls
    # Each URL got the right payload format
    by_url = {c[0]: c[1] for c in posted_calls}
    assert "blocks" in by_url[slack_ep]
    assert by_url[teams_ep]["@type"] == "MessageCard"


def test_deliver_query_includes_teams_protocol(monkeypatch):
    """The subscriber query must include teams-webhook so Teams rows are fetched."""
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")

    captured_sql = []
    def capture_query(sql, params=None):
        captured_sql.append(sql)
        return []

    with patch.object(h, "boto3"):
        h._deliver_report(capture_query, "c1", "2026-06-23", "daily", "s")

    sub_sql = next((s for s in captured_sql if "alert_subscribers_managed" in s), "")
    assert "teams-webhook" in sub_sql


def test_deliver_noop_when_flag_off_teams(monkeypatch):
    """REPORT_DELIVERY_ENABLED gate still applies for teams path."""
    monkeypatch.delenv("REPORT_DELIVERY_ENABLED", raising=False)
    cache_query = MagicMock()
    with patch.object(h, "boto3"):
        h._deliver_report(cache_query, "c1", "2026-06-23", "daily", "요약")
    cache_query.assert_not_called()
