"""Task 4: /api/clusters LIST filtered by team visibility.

Verifies that _handle_list passes the event to tenancy.visible_cluster_ids
and filters accordingly:
  - admin GET -> all 3 clusters returned (None filter = no-op)
  - viewer in team A -> c-open + c-teamA only
  - viewer with no teams -> c-open only
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

_CLUSTERS_DIR = Path(__file__).resolve().parents[3] / "api" / "clusters"
# Push clusters/ dir so `import seeder`, `import engine_family`, `import tenancy` resolve.
sys.path.insert(0, str(_CLUSTERS_DIR))

_PATH = _CLUSTERS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("clusters_handler_tenancy", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def _make_token(groups=None, username="u-alice"):
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _admin_event(method="GET", path="/api/clusters"):
    token = _make_token(groups=["dbops-admin"], username="u-admin")
    return {
        "httpMethod": method,
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
    }


def _viewer_event(method="GET", path="/api/clusters", username="u-viewer", groups=None):
    if groups is None:
        groups = ["dbops-viewer"]
    token = _make_token(groups=groups, username=username)
    return {
        "httpMethod": method,
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

_ITEMS = [
    {"cluster_id": "c-open"},
    {"cluster_id": "c-teamA", "team_id": "tA"},
    {"cluster_id": "c-teamB", "team_id": "tB"},
]


def test_admin_sees_all_three(monkeypatch):
    """Admin GET /api/clusters -> visible_cluster_ids returns None -> all items returned."""
    monkeypatch.setattr(handler, "_scan_all", lambda table, **k: list(_ITEMS))
    monkeypatch.setattr(handler, "_enrich_with_meta", lambda x: x)
    # visible_cluster_ids returns None for admin => no filter applied
    monkeypatch.setattr(handler.tenancy, "visible_cluster_ids", lambda ev, items: None)

    ev = _admin_event()
    r = handler.lambda_handler(ev, None)
    assert r["statusCode"] == 200
    ids = {c["cluster_id"] for c in json.loads(r["body"])}
    assert ids == {"c-open", "c-teamA", "c-teamB"}


def test_viewer_in_team_a_sees_open_and_team_a(monkeypatch):
    """Viewer in team A -> c-open + c-teamA only."""
    monkeypatch.setattr(handler, "_scan_all", lambda table, **k: list(_ITEMS))
    monkeypatch.setattr(handler, "_enrich_with_meta", lambda x: x)
    monkeypatch.setattr(handler.tenancy, "my_team_ids", lambda u: {"tA"})

    ev = _viewer_event()
    r = handler.lambda_handler(ev, None)
    assert r["statusCode"] == 200
    ids = {c["cluster_id"] for c in json.loads(r["body"])}
    assert ids == {"c-open", "c-teamA"}


def test_viewer_no_teams_sees_only_open(monkeypatch):
    """Viewer with no team memberships -> only unassigned clusters."""
    monkeypatch.setattr(handler, "_scan_all", lambda table, **k: list(_ITEMS))
    monkeypatch.setattr(handler, "_enrich_with_meta", lambda x: x)
    monkeypatch.setattr(handler.tenancy, "my_team_ids", lambda u: set())

    ev = _viewer_event()
    r = handler.lambda_handler(ev, None)
    assert r["statusCode"] == 200
    ids = {c["cluster_id"] for c in json.loads(r["body"])}
    assert ids == {"c-open"}
