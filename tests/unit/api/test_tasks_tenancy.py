"""Task 4: /api/tasks visibility gate.

tasks LIST (GET /api/tasks, no ?cluster): filter to visible set.
tasks GET /{id}: 403 when item's cluster_id is not visible.
tasks POST: 403 when body.cluster_id is not in visible set.
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
# Module loading — push api/tasks on sys.path so `import tenancy` resolves
# ---------------------------------------------------------------------------

_TASKS_DIR = Path(__file__).resolve().parents[3] / "api" / "tasks"
sys.path.insert(0, str(_TASKS_DIR))

# Stub env vars needed at module import time
os.environ.setdefault("AGENT_TASKS_TABLE", "agent-tasks-stub")
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")

_PATH = _TASKS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("tasks_handler_tenancy", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENT_TASKS_TABLE", "agent-tasks-stub")
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


def _viewer_event(method="GET", path="/api/tasks", path_params=None, qsp=None, body=None):
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


def _admin_event(method="GET", path="/api/tasks", path_params=None, qsp=None, body=None):
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

_LIST_ITEMS = [
    {"task_id": "t1", "cluster_id": "c-open",  "status": "done"},
    {"task_id": "t2", "cluster_id": "c-teamA", "status": "done"},
    {"task_id": "t3", "cluster_id": "c-teamB", "status": "done"},
]


def test_tasks_list_viewer_excludes_other_team(monkeypatch):
    """Viewer: items for c-open/c-teamA/c-teamB; visible_set = {c-open, c-teamA}
    => c-teamB excluded."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": _LIST_ITEMS}
    monkeypatch.setattr(handler.tenancy, "visible_set_from_registry",
                        lambda ev: {"c-open", "c-teamA"})
    with patch.object(handler, "_table", return_value=mock_table):
        r = handler.lambda_handler(_viewer_event(), None)
    assert r["statusCode"] == 200
    tasks = json.loads(r["body"])["tasks"]
    cluster_ids = {t["cluster_id"] for t in tasks}
    assert "c-teamB" not in cluster_ids
    assert "c-open" in cluster_ids
    assert "c-teamA" in cluster_ids


def test_tasks_list_admin_sees_all(monkeypatch):
    """Admin: visible_set_from_registry returns None => all rows pass through."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": _LIST_ITEMS}
    monkeypatch.setattr(handler.tenancy, "visible_set_from_registry",
                        lambda ev: None)
    with patch.object(handler, "_table", return_value=mock_table):
        r = handler.lambda_handler(_admin_event(), None)
    assert r["statusCode"] == 200
    tasks = json.loads(r["body"])["tasks"]
    cluster_ids = {t["cluster_id"] for t in tasks}
    assert cluster_ids == {"c-open", "c-teamA", "c-teamB"}


def test_tasks_list_with_cluster_param_bypasses_filter(monkeypatch):
    """When ?cluster is specified, the existing per-cluster GSI query fires;
    the global filter only applies to the all-tasks query (no ?cluster)."""
    mock_table = MagicMock()
    mock_table.query.return_value = {"Items": [{"task_id": "t3", "cluster_id": "c-teamB"}]}
    # Even if visible_set would exclude c-teamB, a ?cluster query is a direct
    # cluster-scoped GSI lookup. The filter still applies — assert filter is consistent.
    monkeypatch.setattr(handler.tenancy, "visible_set_from_registry",
                        lambda ev: {"c-open", "c-teamA"})
    with patch.object(handler, "_table", return_value=mock_table):
        r = handler.lambda_handler(_viewer_event(qsp={"cluster": "c-teamB"}), None)
    # The filter should exclude c-teamB even in the ?cluster case
    assert r["statusCode"] == 200
    tasks = json.loads(r["body"])["tasks"]
    cluster_ids = {t["cluster_id"] for t in tasks}
    assert "c-teamB" not in cluster_ids


# ---------------------------------------------------------------------------
# GET /{id} tests
# ---------------------------------------------------------------------------

def test_tasks_get_by_id_forbidden_for_viewer(monkeypatch):
    """Viewer reads task whose cluster_id is c-teamB; cluster_visible=False => 403."""
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"task_id": "t3", "cluster_id": "c-teamB", "status": "done"}}
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    with patch.object(handler, "_table", return_value=mock_table):
        r = handler.lambda_handler(
            _viewer_event(path="/api/tasks/t3", path_params={"id": "t3"}), None
        )
    assert r["statusCode"] == 403
    body = json.loads(r["body"])
    assert "접근 권한" in body.get("error", "")


def test_tasks_get_by_id_allowed_when_visible(monkeypatch):
    """cluster_visible=True => 200 (not 403)."""
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"task_id": "t2", "cluster_id": "c-teamA", "status": "done"}}
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tA"})
    with patch.object(handler, "_table", return_value=mock_table):
        r = handler.lambda_handler(
            _viewer_event(path="/api/tasks/t2", path_params={"id": "t2"}), None
        )
    assert r["statusCode"] != 403


def test_tasks_get_by_id_admin_not_gated(monkeypatch):
    """Admin: cluster_visible returns True => 200 always."""
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": {"task_id": "t3", "cluster_id": "c-teamB", "status": "done"}}
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    with patch.object(handler, "_table", return_value=mock_table):
        r = handler.lambda_handler(
            _admin_event(path="/api/tasks/t3", path_params={"id": "t3"}), None
        )
    assert r["statusCode"] != 403


# ---------------------------------------------------------------------------
# POST tests
# ---------------------------------------------------------------------------

def test_tasks_post_non_visible_cluster_id_forbidden(monkeypatch):
    """POST with a non-visible cluster_id => 403."""
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    # cluster must exist so we get past the existence check
    monkeypatch.setattr(handler, "_cluster_exists", lambda cid: True)
    r = handler.lambda_handler(
        _viewer_event(method="POST", body={"cluster_id": "c-teamB", "kind": "manual_rca"}), None
    )
    assert r["statusCode"] == 403
    body = json.loads(r["body"])
    assert "접근 권한" in body.get("error", "")


def test_tasks_post_visible_cluster_id_allowed(monkeypatch):
    """POST with a visible cluster_id => 201."""
    mock_table = MagicMock()
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tA"})
    monkeypatch.setattr(handler, "_cluster_exists", lambda cid: True)
    with patch.object(handler, "_table", return_value=mock_table):
        r = handler.lambda_handler(
            _viewer_event(method="POST", body={"cluster_id": "c-teamA", "kind": "manual_rca"}), None
        )
    assert r["statusCode"] == 201


def test_tasks_post_admin_not_gated(monkeypatch):
    """Admin POST => cluster_visible returns True => 201."""
    mock_table = MagicMock()
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    monkeypatch.setattr(handler, "_cluster_exists", lambda cid: True)
    with patch.object(handler, "_table", return_value=mock_table):
        r = handler.lambda_handler(
            _admin_event(method="POST", body={"cluster_id": "c-teamB", "kind": "manual_rca"}), None
        )
    assert r["statusCode"] == 201
