"""Unit tests for agent/tenancy.py — visible_cluster_ids_for."""

import base64
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

_MOD = Path(__file__).resolve().parents[3] / "agent" / "tenancy.py"


def _load():
    spec = importlib.util.spec_from_file_location("agent_tenancy", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _headers(groups=None, username="u-alice", with_bearer=True):
    """Build a headers dict with a minimal base64url JWT payload."""
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    tok = f"h.{payload}.s" if with_bearer else ""
    return {"Authorization": f"Bearer {tok}"} if with_bearer else {}


ITEMS = [
    {"cluster_id": "c-open"},                        # unassigned
    {"cluster_id": "c-teamA", "team_id": "tA"},
    {"cluster_id": "c-teamB", "team_id": "tB"},
]


def test_admin_returns_none(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    result = t.visible_cluster_ids_for(_headers(groups=["dbops-admin"]))
    assert result is None


def test_no_groups_is_admin_fallback_returns_none(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    # no groups claim at all => single-admin-deploy fallback => admin => None
    result = t.visible_cluster_ids_for(_headers(groups=None))
    assert result is None


def test_viewer_in_team_a_sees_open_and_team_a_not_b(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    fake = MagicMock()
    fake.scan.return_value = {"Items": ITEMS}
    with patch.object(t.boto3, "resource", return_value=MagicMock(**{"Table.return_value": fake})), \
         patch.object(t, "_my_team_ids", return_value={"tA"}):
        vis = t.visible_cluster_ids_for(_headers(groups=["dbops-viewer"]))
    assert vis == {"c-open", "c-teamA"}


def test_viewer_no_teams_sees_only_unassigned(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    fake = MagicMock()
    fake.scan.return_value = {"Items": ITEMS}
    with patch.object(t.boto3, "resource", return_value=MagicMock(**{"Table.return_value": fake})), \
         patch.object(t, "_my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids_for(_headers(groups=["dbops-viewer"]))
    assert vis == {"c-open"}


def test_no_authorization_header_returns_unassigned_only(monkeypatch):
    """No/empty Authorization => viewer-with-no-teams => unassigned only, NOT None."""
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    fake = MagicMock()
    fake.scan.return_value = {"Items": ITEMS}
    with patch.object(t.boto3, "resource", return_value=MagicMock(**{"Table.return_value": fake})), \
         patch.object(t, "_my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids_for({})
    assert vis == {"c-open"}
    assert vis is not None


def test_scan_error_fails_open_none(monkeypatch):
    """Registry scan raises => None (fail-open, never break chat on DDB outage)."""
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with patch.object(t.boto3, "resource", side_effect=RuntimeError("ddb down")):
        result = t.visible_cluster_ids_for(_headers(groups=["dbops-viewer"]))
    assert result is None
