"""Task 4: /api/scheduled-tasks visibility gate.

scheduled_tasks LIST (GET /api/scheduled-tasks, no ?cluster): filter to visible set.
scheduled_tasks POST: 403 when body.cluster_id is not in visible set.
scheduled_tasks DELETE /{id}: 403 when the fetched task's cluster_id is not visible.
Admin -> no-op (all rows / no 403).
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module loading — push api/scheduled_tasks on sys.path so `import tenancy` resolves
# ---------------------------------------------------------------------------

_SCHED_DIR = Path(__file__).resolve().parents[3] / "api" / "scheduled_tasks"
sys.path.insert(0, str(_SCHED_DIR))

# Stub env vars needed at module import time
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")

_PATH = _SCHED_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("scheduled_tasks_handler_tenancy", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
    monkeypatch.setenv("CACHE_DB_NAME", "dbops")
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    monkeypatch.setenv("TEAM_MEMBERS_TABLE", "team-members-stub")
    monkeypatch.setenv("TEAM_MEMBERS_BY_USER_INDEX", "by-user")


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def _make_token(groups=None, username="u-test"):
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _viewer_event(method="GET", path="/api/scheduled-tasks", path_params=None, qsp=None, body=None):
    token = _make_token(groups=["dbops-viewer"], username="u-viewer")
    ev = {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
        "body": json.dumps(body) if body is not None else None,
    }
    return ev


def _admin_event(method="GET", path="/api/scheduled-tasks", path_params=None, qsp=None, body=None):
    token = _make_token(groups=["dbops-admin"], username="u-admin")
    ev = {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
        "body": json.dumps(body) if body is not None else None,
    }
    return ev


# ---------------------------------------------------------------------------
# LIST tests
# ---------------------------------------------------------------------------

_LIST_ROWS = [
    {"id": 1, "cluster_id": "c-open",  "kind": "scheduled_report", "interval_kind": "daily"},
    {"id": 2, "cluster_id": "c-teamA", "kind": "scheduled_report", "interval_kind": "daily"},
    {"id": 3, "cluster_id": "c-teamB", "kind": "scheduled_report", "interval_kind": "daily"},
]


def test_scheduled_tasks_list_viewer_excludes_other_team(monkeypatch):
    """Viewer: rows for c-open/c-teamA/c-teamB; visible_set = {c-open, c-teamA}
    => c-teamB excluded."""
    monkeypatch.setattr(handler.tenancy, "visible_set_from_registry",
                        lambda ev: {"c-open", "c-teamA"})
    with patch.object(handler, "_query", return_value=_LIST_ROWS):
        r = handler.lambda_handler(_viewer_event(), None)
    assert r["statusCode"] == 200
    schedules = json.loads(r["body"])["schedules"]
    cluster_ids = {s["cluster_id"] for s in schedules}
    assert "c-teamB" not in cluster_ids
    assert "c-open" in cluster_ids
    assert "c-teamA" in cluster_ids


def test_scheduled_tasks_list_admin_sees_all(monkeypatch):
    """Admin: visible_set_from_registry returns None => all rows pass through."""
    monkeypatch.setattr(handler.tenancy, "visible_set_from_registry",
                        lambda ev: None)
    with patch.object(handler, "_query", return_value=_LIST_ROWS):
        r = handler.lambda_handler(_admin_event(), None)
    assert r["statusCode"] == 200
    schedules = json.loads(r["body"])["schedules"]
    cluster_ids = {s["cluster_id"] for s in schedules}
    assert cluster_ids == {"c-open", "c-teamA", "c-teamB"}


# ---------------------------------------------------------------------------
# POST tests
# ---------------------------------------------------------------------------

def test_scheduled_tasks_post_non_visible_cluster_id_forbidden(monkeypatch):
    """POST with a non-visible cluster_id => 403."""
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    monkeypatch.setattr(handler, "_cluster_exists", lambda cid: True)
    r = handler.lambda_handler(
        _viewer_event(method="POST",
                      body={"cluster_id": "c-teamB", "interval_kind": "daily", "kind": "scheduled_report"}),
        None,
    )
    assert r["statusCode"] == 403
    body = json.loads(r["body"])
    assert "접근 권한" in body.get("error", "")


def test_scheduled_tasks_post_visible_cluster_id_allowed(monkeypatch):
    """POST with a visible cluster_id => 201."""
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tA"})
    monkeypatch.setattr(handler, "_cluster_exists", lambda cid: True)
    with patch.object(handler, "_query", return_value=[{"id": 9}]):
        r = handler.lambda_handler(
            _viewer_event(method="POST",
                          body={"cluster_id": "c-teamA", "interval_kind": "daily", "kind": "scheduled_report"}),
            None,
        )
    assert r["statusCode"] == 201


def test_scheduled_tasks_post_admin_not_gated(monkeypatch):
    """Admin POST => cluster_visible returns True => 201."""
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    monkeypatch.setattr(handler, "_cluster_exists", lambda cid: True)
    with patch.object(handler, "_query", return_value=[{"id": 10}]):
        r = handler.lambda_handler(
            _admin_event(method="POST",
                         body={"cluster_id": "c-teamB", "interval_kind": "daily", "kind": "scheduled_report"}),
            None,
        )
    assert r["statusCode"] == 201


# ---------------------------------------------------------------------------
# DELETE tests
# ---------------------------------------------------------------------------

def test_scheduled_tasks_delete_non_visible_cluster_forbidden(monkeypatch):
    """DELETE /{id}: fetch the task first, gate on its cluster_id => 403."""
    # First _query call returns the row for the id; second would be the DELETE
    row = {"id": 3, "cluster_id": "c-teamB", "kind": "scheduled_report"}
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    with patch.object(handler, "_query", return_value=[row]):
        r = handler.lambda_handler(
            _viewer_event(method="DELETE", path="/api/scheduled-tasks/3",
                          path_params={"id": "3"}),
            None,
        )
    assert r["statusCode"] == 403
    body = json.loads(r["body"])
    assert "접근 권한" in body.get("error", "")


def test_scheduled_tasks_delete_visible_cluster_allowed(monkeypatch):
    """DELETE /{id}: cluster_visible=True => 200."""
    row = {"id": 2, "cluster_id": "c-teamA", "kind": "scheduled_report"}
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tA"})
    with patch.object(handler, "_query", return_value=[row]):
        r = handler.lambda_handler(
            _viewer_event(method="DELETE", path="/api/scheduled-tasks/2",
                          path_params={"id": "2"}),
            None,
        )
    assert r["statusCode"] != 403


def test_scheduled_tasks_delete_admin_not_gated(monkeypatch):
    """Admin DELETE => cluster_visible returns True => 200."""
    row = {"id": 3, "cluster_id": "c-teamB", "kind": "scheduled_report"}
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    with patch.object(handler, "_query", return_value=[row]):
        r = handler.lambda_handler(
            _admin_event(method="DELETE", path="/api/scheduled-tasks/3",
                         path_params={"id": "3"}),
            None,
        )
    assert r["statusCode"] != 403
