"""Unit tests for agent/tenancy.py — visible_cluster_ids_for.

Identity comes from a JWKS-VERIFIED Cognito ID token in a custom header. The
real JWKS verification (PyJWKClient + cryptography) can't run in this test env,
so `_verify_token` is patched to simulate verified/failed verification; these
tests cover the header parsing + the visibility logic + the security property
that an UNVERIFIED token grants no escalation.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_MOD = Path(__file__).resolve().parents[3] / "agent" / "tenancy.py"
_CUSTOM = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Authorization"


def _load():
    spec = importlib.util.spec_from_file_location("agent_tenancy", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _hdr(token="dummy"):
    """Headers carrying the custom identity token (the value is irrelevant —
    tests patch _verify_token to control the resulting claims)."""
    return {_CUSTOM: f"Bearer {token}"}


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
        assert t.visible_cluster_ids_for(_hdr()) is None


def test_verified_no_groups_admin_fallback_returns_none(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    # verified token, no groups claim => single-admin fallback => admin => None
    with patch.object(t, "_verify_token", return_value={"sub": "a"}):
        assert t.visible_cluster_ids_for(_hdr()) is None


def test_verified_viewer_in_team_a_sees_open_and_team_a_not_b(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with _fake_ddb(t), \
         patch.object(t, "_verify_token", return_value={"cognito:groups": ["dbops-viewer"], "cognito:username": "u-a"}), \
         patch.object(t, "_my_team_ids", return_value={"tA"}):
        vis = t.visible_cluster_ids_for(_hdr())
    assert vis == {"c-open", "c-teamA"}


def test_verified_viewer_no_teams_sees_only_unassigned(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with _fake_ddb(t), \
         patch.object(t, "_verify_token", return_value={"cognito:groups": ["dbops-viewer"], "cognito:username": "u-a"}), \
         patch.object(t, "_my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids_for(_hdr())
    assert vis == {"c-open"}


def test_no_custom_header_returns_unassigned_only(monkeypatch):
    """No identity header => verify never trusts anything => unassigned only."""
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with _fake_ddb(t), patch.object(t, "_my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids_for({})
    assert vis == {"c-open"}


def test_unverified_token_grants_no_escalation(monkeypatch):
    """SECURITY: a custom-header token that FAILS verification (e.g. a forged
    admin token) yields {} claims => treated as no-team viewer => unassigned
    only. A forged admin token must NOT return None (all)."""
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with _fake_ddb(t), \
         patch.object(t, "_verify_token", return_value={}), \
         patch.object(t, "_my_team_ids", return_value=set()):
        vis = t.visible_cluster_ids_for(_hdr("forged.admin.token"))
    assert vis == {"c-open"}
    assert vis is not None


def test_scan_error_fails_open_none(monkeypatch):
    """Registry scan raises => None (fail-open, never break chat on DDB outage)."""
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with patch.object(t, "_verify_token", return_value={"cognito:groups": ["dbops-viewer"], "sub": "u"}), \
         patch.object(t.boto3, "resource", side_effect=RuntimeError("ddb down")):
        assert t.visible_cluster_ids_for(_hdr()) is None


def test_claims_from_headers_ignores_plain_authorization(monkeypatch):
    """The consumed Authorization header is NOT the identity source — only the
    custom header is. A plain Authorization header yields no claims."""
    t = _load()
    # plain Authorization (no custom header) => no token read => {}
    assert t._claims_from_headers({"Authorization": "Bearer x.y.z"}) == {}


def test_claims_from_headers_reads_custom_header(monkeypatch):
    t = _load()
    with patch.object(t, "_verify_token", return_value={"sub": "u-1"}) as v:
        out = t._claims_from_headers({_CUSTOM: "Bearer the-token"})
    assert out == {"sub": "u-1"}
    v.assert_called_once_with("the-token")


def test_claims_from_headers_requires_bearer(monkeypatch):
    t = _load()
    assert t._claims_from_headers({_CUSTOM: "the-token-without-bearer"}) == {}
