"""Task 8: /api/learning overview endpoint.

Tests that _learning_overview groups fleet rows (cluster_id='*') vs
per-cluster rows and returns recent resolved/persisted cases.
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Module loading — push api/dashboard on sys.path so sibling imports resolve
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_HANDLER_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler", _HANDLER_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

dash = _mod


def teardown_module(_module):
    if str(_DASHBOARD_DIR) in sys.path:
        sys.path.remove(str(_DASHBOARD_DIR))


# ---------------------------------------------------------------------------
# Event builders (mirrors test_alerts_tenancy.py pattern)
# ---------------------------------------------------------------------------

def _make_token(groups=None, username="u-test"):
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _viewer_event(username="u-viewer"):
    token = _make_token(groups=["dbops-viewer"], username=username)
    return {
        "httpMethod": "GET",
        "rawPath": "/api/learning",
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": "GET", "path": "/api/learning"}},
        "queryStringParameters": {},
        "pathParameters": {},
    }


def _admin_event():
    token = _make_token(groups=["dbops-admin"], username="u-admin")
    return {
        "httpMethod": "GET",
        "rawPath": "/api/learning",
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": "GET", "path": "/api/learning"}},
        "queryStringParameters": {},
        "pathParameters": {},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_learning_overview_groups_fleet_and_clusters():
    """Basic grouping: fleet ('*') vs per-cluster, recent cases returned."""
    def query(sql, params=None):
        if "remediation_outcomes_agg" in sql:
            return [
                {
                    "cluster_id": "*",
                    "symptom_class": "anomaly:cpu",
                    "action_class": "manual",
                    "successes": 3,
                    "attempts": 4,
                    "last_outcome": "resolved",
                },
                {
                    "cluster_id": "c1",
                    "symptom_class": "finding:query_regression",
                    "action_class": "index_add",
                    "successes": 2,
                    "attempts": 2,
                    "last_outcome": "resolved",
                },
            ]
        if "FROM remediation_cases" in sql:
            return [
                {
                    "cluster_id": "c1",
                    "symptom_class": "finding:query_regression",
                    "action_class": "index_add",
                    "status": "resolved",
                    "evaluated_at": "t",
                }
            ]
        return []

    # event=None => no tenancy filter (legacy/admin path)
    body = dash._learning_overview(query)
    assert len(body["fleet"]) == 1
    assert body["fleet"][0]["symptom_class"] == "anomaly:cpu"
    assert "c1" in body["clusters"]
    assert len(body["clusters"]["c1"]) == 1
    assert body["recent"][0]["status"] == "resolved"


def test_learning_overview_empty():
    """No rows → empty collections, no crash."""
    body = dash._learning_overview(lambda sql, params=None: [])
    assert body == {"fleet": [], "clusters": {}, "recent": []}


def test_learning_overview_tenancy_scopes_viewer(monkeypatch):
    """Viewer for Team A sees c1 (Team A) but NOT c2 (Team B).
    Fleet '*' rows are always visible regardless of team membership."""

    def query(sql, params=None):
        if "remediation_outcomes_agg" in sql:
            return [
                # fleet aggregate — no real cluster identity
                {"cluster_id": "*",  "symptom_class": "anomaly:cpu",   "action_class": "manual",    "successes": 5, "attempts": 6, "last_outcome": "resolved"},
                # Team A cluster
                {"cluster_id": "c1", "symptom_class": "finding:slow",   "action_class": "index_add", "successes": 2, "attempts": 2, "last_outcome": "resolved"},
                # Team B cluster — should be hidden from Team A viewer
                {"cluster_id": "c2", "symptom_class": "finding:cpu",    "action_class": "manual",    "successes": 1, "attempts": 3, "last_outcome": "persisted"},
            ]
        if "FROM remediation_cases" in sql:
            return [
                {"cluster_id": "c1", "symptom_class": "finding:slow", "action_class": "index_add", "status": "resolved",  "evaluated_at": "2026-06-01T00:00:00Z"},
                {"cluster_id": "c2", "symptom_class": "finding:cpu",  "action_class": "manual",    "status": "persisted", "evaluated_at": "2026-06-02T00:00:00Z"},
            ]
        return []

    # Stub visible_cluster_ids: viewer can only see c1
    monkeypatch.setattr(
        dash.tenancy, "visible_cluster_ids",
        lambda ev, items: {"c1"},
    )
    # Stub _registered_clusters so it returns a non-None registry (triggers filter)
    monkeypatch.setattr(
        dash, "_registered_clusters",
        lambda: {"c1": {"team_id": "team-a"}, "c2": {"team_id": "team-b"}},
    )

    body = dash._learning_overview(query, event=_viewer_event())

    # fleet '*' always visible
    assert len(body["fleet"]) == 1
    assert body["fleet"][0]["cluster_id"] == "*"

    # clusters: c1 visible, c2 hidden
    assert "c1" in body["clusters"]
    assert "c2" not in body["clusters"]

    # recent: only c1's case
    assert all(c["cluster_id"] == "c1" for c in body["recent"])
    assert len(body["recent"]) == 1


def test_learning_overview_tenancy_admin_sees_all(monkeypatch):
    """Admin: visible_cluster_ids returns None => all clusters pass through."""

    def query(sql, params=None):
        if "remediation_outcomes_agg" in sql:
            return [
                {"cluster_id": "c1", "symptom_class": "s", "action_class": "a", "successes": 1, "attempts": 1, "last_outcome": "resolved"},
                {"cluster_id": "c2", "symptom_class": "s", "action_class": "a", "successes": 1, "attempts": 1, "last_outcome": "resolved"},
            ]
        if "FROM remediation_cases" in sql:
            return [
                {"cluster_id": "c1", "symptom_class": "s", "action_class": "a", "status": "resolved", "evaluated_at": "t"},
                {"cluster_id": "c2", "symptom_class": "s", "action_class": "a", "status": "resolved", "evaluated_at": "t"},
            ]
        return []

    # Admin: visible_cluster_ids returns None (unfiltered)
    monkeypatch.setattr(
        dash.tenancy, "visible_cluster_ids",
        lambda ev, items: None,
    )
    monkeypatch.setattr(
        dash, "_registered_clusters",
        lambda: {"c1": {"team_id": "team-a"}, "c2": {"team_id": "team-b"}},
    )

    body = dash._learning_overview(query, event=_admin_event())

    assert "c1" in body["clusters"]
    assert "c2" in body["clusters"]
    assert len(body["recent"]) == 2
