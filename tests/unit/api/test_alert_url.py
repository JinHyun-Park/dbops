"""Unit tests for alert_evaluator URL builder + dedup window math.

These are pure-function smoke tests — no AWS calls, no network. They catch
regressions in:
  - Dashboard deep-link construction (Slack button URL).
  - PagerDuty dedup_key bucketing (TTL window).
  - Graceful degradation when FRONTEND_URL is unset.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "data-pipeline" / "alert_evaluator" / "handler.py"


def _fresh_handler():
    """Load alert_evaluator/handler.py as a uniquely-named module so it
    doesn't collide with other `handler.py` files registered by sibling
    tests (mcp-servers operations etc.). Fresh module = picks up the
    current env vars in its top-level constants/functions."""
    spec = importlib.util.spec_from_file_location("alert_evaluator_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://example.test")
    monkeypatch.setenv("ALERT_DEDUP_WINDOW_MINUTES", "30")
    return _fresh_handler()


def test_dashboard_url_with_frontend(handler):
    rule = {"id": 7, "cluster_id": "prod-pg"}
    url = handler._dashboard_url(rule, "/dashboard")
    assert url == "https://example.test/dashboard?cluster=prod-pg&alert_id=7"


def test_dashboard_url_missing_frontend(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    h = _fresh_handler()
    assert h._dashboard_url({"id": 1, "cluster_id": "c"}) == ""


def test_slack_payload_includes_actions_block(handler):
    rule = {
        "id": 42,
        "cluster_id": "prod-pg",
        "name": "High CPU",
        "metric_type": "cpu",
        "comparison": ">",
        "threshold": 80.0,
    }
    payload = handler._build_slack_payload(rule, 92.4)
    block_types = [b.get("type") for b in payload["blocks"]]
    assert "actions" in block_types, "deep-link buttons should appear when FRONTEND_URL is set"
    actions = next(b for b in payload["blocks"] if b["type"] == "actions")
    urls = [el["url"] for el in actions["elements"]]
    assert any("dashboard?cluster=prod-pg" in u for u in urls)
    assert any("alerts?cluster=prod-pg" in u for u in urls)


def test_slack_payload_omits_actions_when_no_url(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    h = _fresh_handler()
    rule = {"id": 1, "cluster_id": "c", "name": "n", "metric_type": "m", "comparison": ">", "threshold": 0.0}
    payload = h._build_slack_payload(rule, 1.0)
    block_types = [b.get("type") for b in payload["blocks"]]
    assert "actions" not in block_types


def test_pagerduty_dedup_key_uses_window_bucket(handler):
    rule = {
        "id": 99,
        "cluster_id": "c",
        "name": "n",
        "metric_type": "cpu",
        "comparison": ">",
        "threshold": 80.0,
    }
    payload = handler._build_pagerduty_payload(rule, 91.0, "INT-KEY")
    assert payload["dedup_key"].startswith("dbops-rule-99-w"), (
        "dedup_key must carry the window bucket suffix so flapping alerts re-open incidents"
    )


def test_dedup_window_seconds_floors_at_60(handler, monkeypatch):
    monkeypatch.setenv("ALERT_DEDUP_WINDOW_MINUTES", "0")
    assert handler._dedup_window_seconds() >= 60, "floor protects from pathological values"


def test_dedup_window_seconds_default(handler, monkeypatch):
    monkeypatch.delenv("ALERT_DEDUP_WINDOW_MINUTES", raising=False)
    assert handler._dedup_window_seconds() == 30 * 60


def test_pagerduty_links_present_when_frontend_url_set(handler):
    rule = {"id": 5, "cluster_id": "c", "name": "n", "metric_type": "m", "comparison": ">", "threshold": 0.0}
    payload = handler._build_pagerduty_payload(rule, 1.0, "KEY")
    assert len(payload.get("links") or []) == 2
