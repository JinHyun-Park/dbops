"""Task 3: /api/approvals visibility gate.

Approvals LIST (GET /api/approvals): filter rows by visible set.
Approvals activity (GET /api/approvals/activity): filter rows by visible set.
Approvals GET /{id}: 403 when fetched item's cluster_id is not visible.
Admin -> no-op (all rows / no 403).
POST/PUT are admin-gated at handler level -> SKIP (admins see all).
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module loading — push api/approvals on sys.path so `import tenancy` resolves
# ---------------------------------------------------------------------------

_APPROVALS_DIR = Path(__file__).resolve().parents[3] / "api" / "approvals"
sys.path.insert(0, str(_APPROVALS_DIR))

# Stub env vars needed at module import time
os.environ.setdefault("APPROVALS_TABLE", "approvals-stub")
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")

_PATH = _APPROVALS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("approvals_handler_tenancy", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    monkeypatch.setenv("TEAM_MEMBERS_TABLE", "team-members-stub")
    monkeypatch.setenv("TEAM_MEMBERS_BY_USER_INDEX", "by-user")
    monkeypatch.setenv("APPROVALS_TABLE", "approvals-stub")


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def _make_token(groups=None, username="u-test"):
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _viewer_event(path="/api/approvals", method="GET", path_params=None, qsp=None):
    token = _make_token(groups=["dbops-viewer"], username="u-viewer")
    return {
        "httpMethod": method,
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
    }


def _admin_event(path="/api/approvals", method="GET", path_params=None, qsp=None):
    token = _make_token(groups=["dbops-admin"], username="u-admin")
    return {
        "httpMethod": method,
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
    }


# ---------------------------------------------------------------------------
# DDB mock helpers
# ---------------------------------------------------------------------------

_LIST_ROWS = [
    {"approval_id": "a1", "cluster_id": "c-open",  "approval_status": "pending", "created_at": "1000"},
    {"approval_id": "a2", "cluster_id": "c-teamA", "approval_status": "pending", "created_at": "999"},
    {"approval_id": "a3", "cluster_id": "c-teamB", "approval_status": "pending", "created_at": "998"},
]


def _mock_table_scan(rows):
    """Return a mock DDB Table whose scan() paginates with the given rows."""
    tbl = MagicMock()
    tbl.scan.return_value = {"Items": rows}
    return tbl


def _mock_dynamodb(rows):
    """Return a mock boto3 resource whose Table() returns a mock with given scan rows."""
    tbl = _mock_table_scan(rows)
    resource = MagicMock()
    resource.Table.return_value = tbl
    return resource


# ---------------------------------------------------------------------------
# LIST (GET /api/approvals) tests
# ---------------------------------------------------------------------------

def test_approvals_list_viewer_excludes_other_team(monkeypatch):
    """Viewer: rows for c-open/c-teamA/c-teamB; visible_set = {c-open, c-teamA}
    => c-teamB row excluded."""
    monkeypatch.setattr(handler, "boto3", MagicMock(resource=lambda *a, **kw: _mock_dynamodb(_LIST_ROWS)))
    monkeypatch.setattr(
        handler.tenancy, "visible_set_from_registry",
        lambda ev: {"c-open", "c-teamA"},
    )

    r = handler.lambda_handler(_viewer_event(), None)
    assert r["statusCode"] == 200
    items = json.loads(r["body"])
    cluster_ids = {row["cluster_id"] for row in items}
    assert "c-teamB" not in cluster_ids
    assert "c-open" in cluster_ids
    assert "c-teamA" in cluster_ids


def test_approvals_list_admin_sees_all(monkeypatch):
    """Admin: visible_set_from_registry returns None => all rows pass through."""
    monkeypatch.setattr(handler, "boto3", MagicMock(resource=lambda *a, **kw: _mock_dynamodb(_LIST_ROWS)))
    monkeypatch.setattr(
        handler.tenancy, "visible_set_from_registry",
        lambda ev: None,
    )

    r = handler.lambda_handler(_admin_event(), None)
    assert r["statusCode"] == 200
    items = json.loads(r["body"])
    cluster_ids = {row["cluster_id"] for row in items}
    assert cluster_ids == {"c-open", "c-teamA", "c-teamB"}


# ---------------------------------------------------------------------------
# Activity (GET /api/approvals/activity) tests
# ---------------------------------------------------------------------------

def test_approvals_activity_viewer_excludes_other_team(monkeypatch):
    """Activity feed: viewer sees only rows from visible clusters."""
    monkeypatch.setattr(handler, "boto3", MagicMock(resource=lambda *a, **kw: _mock_dynamodb(_LIST_ROWS)))
    monkeypatch.setattr(
        handler.tenancy, "visible_set_from_registry",
        lambda ev: {"c-open", "c-teamA"},
    )

    r = handler.lambda_handler(_viewer_event(path="/api/approvals/activity"), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    items = body.get("items", [])
    cluster_ids = {row["cluster_id"] for row in items}
    assert "c-teamB" not in cluster_ids


def test_approvals_activity_admin_sees_all(monkeypatch):
    """Activity feed: admin gets all rows unfiltered."""
    monkeypatch.setattr(handler, "boto3", MagicMock(resource=lambda *a, **kw: _mock_dynamodb(_LIST_ROWS)))
    monkeypatch.setattr(
        handler.tenancy, "visible_set_from_registry",
        lambda ev: None,
    )

    r = handler.lambda_handler(_admin_event(path="/api/approvals/activity"), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    items = body.get("items", [])
    cluster_ids = {row["cluster_id"] for row in items}
    assert cluster_ids == {"c-open", "c-teamA", "c-teamB"}


# ---------------------------------------------------------------------------
# GET /{id} tests
# ---------------------------------------------------------------------------

def test_approvals_get_by_id_forbidden_for_viewer(monkeypatch):
    """Viewer reads approval whose cluster_id is c-teamB; cluster_visible=False => 403."""
    item = {"approval_id": "a3", "cluster_id": "c-teamB", "approval_status": "pending", "created_at": "998"}
    tbl = MagicMock()
    tbl.get_item.return_value = {"Item": item}
    tbl.scan.return_value = {"Items": [item]}
    resource = MagicMock()
    resource.Table.return_value = tbl
    monkeypatch.setattr(handler, "boto3", MagicMock(resource=lambda *a, **kw: resource))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})

    r = handler.lambda_handler(
        _viewer_event("/api/approvals/a3", path_params={"id": "a3"}), None
    )
    assert r["statusCode"] == 403
    body = json.loads(r["body"])
    assert "접근 권한" in body.get("error", "")


def test_approvals_get_by_id_allowed_when_visible(monkeypatch):
    """cluster_visible=True => 200 (not 403)."""
    item = {"approval_id": "a2", "cluster_id": "c-teamA", "approval_status": "pending", "created_at": "999"}
    tbl = MagicMock()
    tbl.get_item.return_value = {"Item": item}
    tbl.scan.return_value = {"Items": [item]}
    resource = MagicMock()
    resource.Table.return_value = tbl
    monkeypatch.setattr(handler, "boto3", MagicMock(resource=lambda *a, **kw: resource))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tA"})

    r = handler.lambda_handler(
        _viewer_event("/api/approvals/a2", path_params={"id": "a2"}), None
    )
    assert r["statusCode"] != 403
