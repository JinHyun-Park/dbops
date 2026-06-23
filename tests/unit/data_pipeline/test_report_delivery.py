"""Tests for _deliver_report in report_generator/handler.py.

Covers: flag-off no-op, SNS publish when enabled, Slack POST to managed
subscribers, and exception swallowing (delivery failure must not raise).
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = (
    Path(__file__).resolve().parents[3]
    / "data-pipeline"
    / "report_generator"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("report_generator.handler", _HANDLER_PATH)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


def test_deliver_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("REPORT_DELIVERY_ENABLED", raising=False)
    cache_query = MagicMock()
    with patch.object(h, "boto3") as mboto:
        h._deliver_report(cache_query, "c1", "2026-06-23", "daily", "요약")
    mboto.client.assert_not_called()   # no SNS
    cache_query.assert_not_called()    # no subscriber read


def test_deliver_sns_when_enabled_no_slack_subs(monkeypatch):
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:::t")
    cache_query = MagicMock(return_value=[])  # no managed slack subs
    sns = MagicMock()
    with patch.object(h, "boto3") as mboto, patch.object(h, "_post_json") as mpost:
        mboto.client.return_value = sns
        h._deliver_report(cache_query, "c1", "2026-06-23", "daily", "요약")
    sns.publish.assert_called_once()
    mpost.assert_not_called()          # no slack subscribers → no POST


def test_deliver_posts_to_slack_subs(monkeypatch):
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:::t")
    cache_query = MagicMock(return_value=[{"id": 1, "protocol": "slack-webhook", "endpoint": "https://hooks.slack/x"}])
    with patch.object(h, "boto3"), patch.object(h, "_post_json", return_value=(200, "ok")) as mpost:
        h._deliver_report(cache_query, "c1", "2026-06-23", "daily", "요약")
    mpost.assert_called_once()


def test_delivery_exception_is_swallowed(monkeypatch):
    monkeypatch.setenv("REPORT_DELIVERY_ENABLED", "true")
    cache_query = MagicMock(side_effect=RuntimeError("db down"))
    with patch.object(h, "boto3"):
        h._deliver_report(cache_query, "c1", "2026-06-23", "daily", "요약")  # must not raise
