"""Fail-closed tenancy: fleet endpoints must not leak data when the DynamoDB
registry scan fails for a non-admin caller.

Covers _learning_overview and _multi_cluster_overview.
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loading — push api/dashboard on sys.path so sibling imports resolve
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_HANDLER_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler_fc", _HANDLER_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

dash = _mod


def teardown_module(_module):
    if str(_DASHBOARD_DIR) in sys.path:
        sys.path.remove(str(_DASHBOARD_DIR))


# ---------------------------------------------------------------------------
# Event builders (same pattern as test_alerts_tenancy.py)
# ---------------------------------------------------------------------------

def _make_token(groups=None, username="u-test"):
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _viewer_event():
    token = _make_token(groups=["dbops-viewer"], username="u-viewer")
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
# Stub query helpers
# ---------------------------------------------------------------------------

def _learning_query_with_clusters(sql, params=None):
    """Returns rows for c1, c2, and fleet '*'."""
    if "remediation_outcomes_agg" in sql:
        return [
            {"cluster_id": "*",  "symptom_class": "anomaly:cpu",  "action_class": "manual",    "successes": 5, "attempts": 6, "last_outcome": "resolved"},
            {"cluster_id": "c1", "symptom_class": "finding:slow", "action_class": "index_add", "successes": 2, "attempts": 2, "last_outcome": "resolved"},
            {"cluster_id": "c2", "symptom_class": "finding:cpu",  "action_class": "manual",    "successes": 1, "attempts": 3, "last_outcome": "persisted"},
        ]
    if "FROM remediation_cases" in sql:
        return [
            {"cluster_id": "c1", "symptom_class": "s", "action_class": "a", "status": "resolved",  "evaluated_at": "2026-06-01T00:00:00Z"},
            {"cluster_id": "c2", "symptom_class": "s", "action_class": "a", "status": "persisted", "evaluated_at": "2026-06-02T00:00:00Z"},
        ]
    return []


def _multi_query_with_clusters(sql, params=None):
    """Returns metric rows for c1 and c2."""
    return [
        {"cluster_id": "c1", "engine": "aurora-postgresql", "engine_version": "15.4",
         "status": "available", "storage_size_gb": 100,
         "cpu": 10.0, "aas": 1.0, "conn_active": 5, "conn_idle": 2,
         "storage_bytes": None, "deadlocks": 0, "blocking_count": 0},
        {"cluster_id": "c2", "engine": "aurora-mysql", "engine_version": "8.0.28",
         "status": "available", "storage_size_gb": 200,
         "cpu": 20.0, "aas": 2.0, "conn_active": 10, "conn_idle": 4,
         "storage_bytes": None, "deadlocks": 0, "blocking_count": 0},
    ]


# ---------------------------------------------------------------------------
# _learning_overview tests
# ---------------------------------------------------------------------------

def test_learning_failclosed_nonadmin_registry_none(monkeypatch):
    """Non-admin + registry scan failure => no real cluster_ids leak.
    Fleet '*' aggregate may still appear (it's anonymized, no real cluster_id).
    """
    monkeypatch.setattr(dash, "_registered_clusters", lambda: None)

    body = dash._learning_overview(_learning_query_with_clusters, event=_viewer_event())

    real_cluster_ids = set(body["clusters"].keys())
    assert real_cluster_ids == set(), (
        f"Fail-open leak: non-admin saw clusters {real_cluster_ids} when registry unavailable"
    )
    recent_cluster_ids = {c.get("cluster_id") for c in body["recent"]}
    assert real_cluster_ids | (recent_cluster_ids - {"*"}) == set(), (
        f"Fail-open leak in recent: {recent_cluster_ids}"
    )


def test_learning_admin_unaffected_by_registry_none(monkeypatch):
    """Admin + registry scan failure => all rows still returned (no filtering)."""
    monkeypatch.setattr(dash, "_registered_clusters", lambda: None)

    body = dash._learning_overview(_learning_query_with_clusters, event=_admin_event())

    assert "c1" in body["clusters"]
    assert "c2" in body["clusters"]


def test_learning_normal_scoping_intact(monkeypatch):
    """Non-admin + registry available => team-scoped filtering works normally
    (c1 in caller's team visible, c2 in other team hidden)."""
    monkeypatch.setattr(
        dash, "_registered_clusters",
        lambda: {"c1": {"team_id": "team-a"}, "c2": {"team_id": "team-b"}},
    )
    monkeypatch.setattr(
        dash.tenancy, "visible_cluster_ids",
        lambda ev, items: {"c1"},
    )

    body = dash._learning_overview(_learning_query_with_clusters, event=_viewer_event())

    assert "c1" in body["clusters"]
    assert "c2" not in body["clusters"]
    assert all(c["cluster_id"] == "c1" for c in body["recent"])


# ---------------------------------------------------------------------------
# _multi_cluster_overview tests
# ---------------------------------------------------------------------------

def test_multi_failclosed_nonadmin_registry_none(monkeypatch):
    """Non-admin + registry scan failure => clusters list is empty."""
    monkeypatch.setattr(dash, "_registered_clusters", lambda: None)

    body = dash._multi_cluster_overview(_multi_query_with_clusters, event=_viewer_event())

    cluster_ids = [r.get("cluster_id") for r in body["clusters"]]
    assert cluster_ids == [], (
        f"Fail-open leak: non-admin saw {cluster_ids} when registry unavailable"
    )


def test_multi_admin_unaffected_by_registry_none(monkeypatch):
    """Admin + registry scan failure => all rows returned."""
    monkeypatch.setattr(dash, "_registered_clusters", lambda: None)

    body = dash._multi_cluster_overview(_multi_query_with_clusters, event=_admin_event())

    cluster_ids = {r.get("cluster_id") for r in body["clusters"]}
    assert "c1" in cluster_ids
    assert "c2" in cluster_ids


def test_multi_normal_scoping_intact(monkeypatch):
    """Non-admin + registry available => c1 visible, c2 hidden."""
    monkeypatch.setattr(
        dash, "_registered_clusters",
        lambda: {"c1": {"team_id": "team-a"}, "c2": {"team_id": "team-b"}},
    )
    monkeypatch.setattr(
        dash.tenancy, "visible_cluster_ids",
        lambda ev, items: {"c1"},
    )

    body = dash._multi_cluster_overview(_multi_query_with_clusters, event=_viewer_event())

    cluster_ids = {r.get("cluster_id") for r in body["clusters"]}
    assert "c1" in cluster_ids
    assert "c2" not in cluster_ids
