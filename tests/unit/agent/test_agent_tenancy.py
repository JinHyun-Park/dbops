"""Unit tests for agent/tenancy.py — visible_cluster_ids_for.

Identity comes from a JWKS-VERIFIED Cognito ID token passed in the invocation
payload. The real JWKS verification (PyJWKClient + cryptography) can't run in
this test env, so `_verify_token` is patched to simulate verified/failed
verification; these tests cover the visibility logic + the security property
that an UNVERIFIED token grants no escalation.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_MOD = Path(__file__).resolve().parents[3] / "agent" / "tenancy.py"


def _load():
    spec = importlib.util.spec_from_file_location("agent_tenancy", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ITEMS = [
    {"cluster_id": "c-open"},                        # unassigned
    {"cluster_id": "c-teamA", "team_id": "tA"},
    {"cluster_id": "c-teamB", "team_id": "tB"},
]


def _fake_ddb(t):
    fake = MagicMock()
    fake.scan.return_value = {"Items": ITEMS}
    return patch.object(t.boto3, "resource", return_value=MagicMock(**{"Table.return_value": fake}))


def test_verified_admin_returns_none(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with patch.object(t, "_verify_token", return_value={"cognito:groups": ["dbops-admin"], "sub": "a"}):
        assert t.visible_cluster_ids_for("tok") is None


def test_verified_no_groups_admin_fallback_returns_none(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    # verified token, no groups claim => single-admin fallback => admin => None
    with patch.object(t, "_verify_token", return_value={"sub": "a"}):
        assert t.visible_cluster_ids_for("tok") is None


def test_verified_viewer_in_team_a_sees_open_and_team_a_not_b(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with _fake_ddb(t), \
         patch.object(t, "_verify_token", return_value={"cognito:groups": ["dbops-viewer"], "cognito:username": "u-a"}), \
         patch.object(t, "_my_team_ids", return_value={"tA"}):
        vis = t.visible_cluster_ids_for("tok")
    assert vis == {"c-open", "c-teamA"}


def test_verified_viewer_no_teams_sees_only_unassigned(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with _fake_ddb(t), \
         patch.object(t, "_verify_token", return_value={"cognito:groups": ["dbops-viewer"], "cognito:username": "u-a"}), \
         patch.object(t, "_my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids_for("tok")
    assert vis == {"c-open"}


def test_no_token_returns_unassigned_only(monkeypatch):
    """No id_token in payload => no trusted claims => unassigned only (NOT all)."""
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with _fake_ddb(t), patch.object(t, "_my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids_for(None)
    assert vis == {"c-open"}


def test_unverified_token_grants_no_escalation(monkeypatch):
    """SECURITY: a token that FAILS verification (e.g. a forged admin token)
    yields {} claims => treated as no-team viewer => unassigned only. A forged
    admin token must NOT return None (all)."""
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with _fake_ddb(t), \
         patch.object(t, "_verify_token", return_value={}), \
         patch.object(t, "_my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids_for("forged.admin.token")
    assert vis == {"c-open"}
    assert vis is not None


def test_scan_error_fails_open_none(monkeypatch):
    """Registry scan raises => None (fail-open, never break chat on DDB outage)."""
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with patch.object(t, "_verify_token", return_value={"cognito:groups": ["dbops-viewer"], "sub": "u"}), \
         patch.object(t.boto3, "resource", side_effect=RuntimeError("ddb down")):
        assert t.visible_cluster_ids_for("tok") is None
