import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_MOD = Path(__file__).resolve().parents[3] / "api" / "clusters" / "tenancy.py"


def _load():
    spec = importlib.util.spec_from_file_location("tenancy_clusters", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _event(groups=None, username="u-alice", with_bearer=True):
    # minimal JWT: header.payload.sig with base64url payload carrying claims
    import base64
    import json
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    tok = f"h.{payload}.s" if with_bearer else ""
    headers = {"authorization": f"Bearer {tok}"} if with_bearer else {}
    return {"headers": headers}


ITEMS = [
    {"cluster_id": "c-open"},                       # unassigned
    {"cluster_id": "c-teamA", "team_id": "tA"},
    {"cluster_id": "c-teamB", "team_id": "tB"},
]


def test_admin_sees_all_returns_none():
    t = _load()
    assert t.visible_cluster_ids(_event(groups=["dbops-admin"]), ITEMS) is None


def test_no_groups_is_admin_fallback():
    t = _load()
    # no groups claim at all => single-admin-deploy fallback => admin => None
    assert t.visible_cluster_ids(_event(groups=None), ITEMS) is None


def test_viewer_no_teams_sees_only_unassigned():
    t = _load()
    with patch.object(t, "my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids(_event(groups=["dbops-viewer"]), ITEMS)
    assert vis == {"c-open"}


def test_viewer_in_team_a_sees_open_plus_team_a_not_b():
    t = _load()
    with patch.object(t, "my_team_ids", return_value={"tA"}):
        vis = t.visible_cluster_ids(_event(groups=["dbops-viewer"]), ITEMS)
    assert vis == {"c-open", "c-teamA"}


def test_cluster_visible_unassigned_true_assigned_member_only():
    t = _load()
    ev = _event(groups=["dbops-viewer"])
    assert t.cluster_visible(ev, {"cluster_id": "x"}) is True          # unassigned
    with patch.object(t, "my_team_ids", return_value={"tA"}):
        assert t.cluster_visible(ev, {"cluster_id": "y", "team_id": "tA"}) is True
        assert t.cluster_visible(ev, {"cluster_id": "z", "team_id": "tB"}) is False


def test_cluster_visible_missing_item_is_default_open():
    t = _load()
    assert t.cluster_visible(_event(groups=["dbops-viewer"]), {}) is True


def test_my_team_ids_infra_error_returns_empty(monkeypatch):
    t = _load()
    monkeypatch.setenv("TEAM_MEMBERS_TABLE", "tbl")
    with patch.object(t.boto3, "resource", side_effect=RuntimeError("ddb down")):
        assert t.my_team_ids("u-alice") == set()


def test_no_bearer_not_admin_restricted():
    t = _load()
    with patch.object(t, "my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids(_event(with_bearer=False), ITEMS)
    assert vis == {"c-open"}   # not admin => restricted, only unassigned


def test_visible_set_from_registry_admin_returns_none(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    assert t.visible_set_from_registry(_event(groups=["dbops-admin"])) is None


def test_visible_set_from_registry_filters_viewer(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    fake = MagicMock()
    fake.scan.return_value = {"Items": [
        {"cluster_id": "c-open"},
        {"cluster_id": "c-teamA", "team_id": "tA"},
        {"cluster_id": "c-teamB", "team_id": "tB"},
    ]}
    with patch.object(t.boto3, "resource", return_value=MagicMock(**{"Table.return_value": fake})), \
         patch.object(t, "my_team_ids", return_value={"tA"}):
        s = t.visible_set_from_registry(_event(groups=["dbops-viewer"]))
    assert s == {"c-open", "c-teamA"}


def test_visible_set_from_registry_scan_error_fails_open_none(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with patch.object(t.boto3, "resource", side_effect=RuntimeError("ddb down")):
        assert t.visible_set_from_registry(_event(groups=["dbops-viewer"])) is None
