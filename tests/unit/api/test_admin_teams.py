"""Tests for the admin Teams management handler (Task 3 — multi-team tenancy)."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

_MOD = Path(__file__).resolve().parents[3] / "api" / "admin_teams" / "handler.py"


def _load():
    spec = importlib.util.spec_from_file_location("admin_teams_handler", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ev(method, path, groups, path_params=None, body=None):
    if groups is not None:
        claims = {"cognito:username": "u-admin", "cognito:groups": list(groups)}
    else:
        claims = {"cognito:username": "u-admin"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "headers": {"authorization": f"Bearer h.{payload}.s"},
        "pathParameters": path_params or {},
        "body": json.dumps(body) if body is not None else None,
    }


def _ev_no_bearer(method, path):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawPath": path,
        "headers": {},
        "pathParameters": {},
        "body": None,
    }


# ---------- admin-gate: fail-closed on every route ----------

def test_viewer_forbidden_on_create():
    t = _load()
    r = t.lambda_handler(_ev("POST", "/api/admin/teams", ["dbops-viewer"], body={"name": "X"}))
    assert r["statusCode"] == 403


def test_viewer_forbidden_on_list():
    t = _load()
    r = t.lambda_handler(_ev("GET", "/api/admin/teams", ["dbops-viewer"]))
    assert r["statusCode"] == 403


def test_viewer_forbidden_on_member_add():
    t = _load()
    r = t.lambda_handler(_ev(
        "POST", "/api/admin/teams/tA/members/bob", ["dbops-viewer"],
        path_params={"team_id": "tA", "username": "bob"},
    ))
    assert r["statusCode"] == 403


def test_viewer_forbidden_on_cluster_assign():
    t = _load()
    r = t.lambda_handler(_ev(
        "POST", "/api/admin/teams/tA/clusters/c1", ["dbops-viewer"],
        path_params={"team_id": "tA", "cluster_id": "c1"},
    ))
    assert r["statusCode"] == 403


def test_viewer_forbidden_on_delete_team():
    t = _load()
    r = t.lambda_handler(_ev(
        "DELETE", "/api/admin/teams/tA", ["dbops-viewer"],
        path_params={"team_id": "tA"},
    ))
    assert r["statusCode"] == 403


def test_no_bearer_denied():
    t = _load()
    r = t.lambda_handler(_ev_no_bearer("GET", "/api/admin/teams"))
    assert r["statusCode"] == 403


def test_raw_token_no_bearer_scheme_denied():
    t = _load()
    claims = {"cognito:username": "u", "cognito:groups": ["dbops-admin"]}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    ev = _ev_no_bearer("GET", "/api/admin/teams")
    ev["headers"]["authorization"] = f"h.{payload}.s"   # missing "Bearer " prefix
    r = t.lambda_handler(ev)
    assert r["statusCode"] == 403


def test_options_bypasses_auth():
    t = _load()
    r = t.lambda_handler(_ev_no_bearer("OPTIONS", "/api/admin/teams"))
    assert r["statusCode"] == 200


def test_no_groups_is_admin():
    """Token with no cognito:groups → implicit admin (matches api/admin_users pattern)."""
    t = _load()
    teams_mock = MagicMock()
    teams_mock.scan.return_value = {"Items": []}
    members_mock = MagicMock()
    with patch.object(t, "_teams_table", return_value=teams_mock), \
         patch.object(t, "_members_table", return_value=members_mock):
        r = t.lambda_handler(_ev("GET", "/api/admin/teams", None))
    assert r["statusCode"] == 200


# ---------- admin happy-path: create team ----------

def test_admin_create_team_writes_row():
    t = _load()
    teams = MagicMock()
    with patch.object(t, "_teams_table", return_value=teams):
        r = t.lambda_handler(_ev("POST", "/api/admin/teams", ["dbops-admin"], body={"name": "Team A"}))
    assert r["statusCode"] in (200, 201)
    body = json.loads(r["body"])
    assert "team_id" in body
    assert teams.put_item.called
    item = teams.put_item.call_args[1]["Item"]
    assert item["name"] == "Team A"
    assert item["team_id"].startswith("team-")


def test_admin_create_team_missing_name_400():
    t = _load()
    r = t.lambda_handler(_ev("POST", "/api/admin/teams", ["dbops-admin"], body={}))
    assert r["statusCode"] == 400


def test_admin_create_team_malformed_body_400():
    t = _load()
    ev = _ev("POST", "/api/admin/teams", ["dbops-admin"])
    ev["body"] = "{not json"
    r = t.lambda_handler(ev)
    assert r["statusCode"] == 400


# ---------- list teams ----------

def test_admin_list_teams_returns_member_counts():
    t = _load()
    teams_mock = MagicMock()
    # paginated scan: one page only (no LastEvaluatedKey)
    teams_mock.scan.return_value = {
        "Items": [
            {"team_id": "tA", "name": "Alpha"},
            {"team_id": "tB", "name": "Beta"},
        ]
    }
    members_mock = MagicMock()

    # boto3 Key("team_id").eq(tid) builds a ConditionBase with _values = (Key(...), tid)
    def query_side(**kw):
        expr = kw.get("KeyConditionExpression")
        val = ""
        try:
            val = expr._values[1]
        except Exception:
            pass
        if val == "tA":
            return {"Items": [{"team_id": "tA", "username": "u1"}, {"team_id": "tA", "username": "u2"}]}
        return {"Items": []}

    members_mock.query.side_effect = query_side

    with patch.object(t, "_teams_table", return_value=teams_mock), \
         patch.object(t, "_members_table", return_value=members_mock):
        r = t.lambda_handler(_ev("GET", "/api/admin/teams", ["dbops-admin"]))
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    by_id = {tm["team_id"]: tm for tm in body["teams"]}
    assert by_id["tA"]["member_count"] == 2
    assert by_id["tB"]["member_count"] == 0


# ---------- member add / remove ----------

def test_admin_add_member_writes_row():
    t = _load()
    teams_mock = MagicMock()
    teams_mock.get_item.return_value = {"Item": {"team_id": "tA", "name": "Alpha"}}
    members_mock = MagicMock()
    with patch.object(t, "_teams_table", return_value=teams_mock), \
         patch.object(t, "_members_table", return_value=members_mock):
        r = t.lambda_handler(_ev(
            "POST", "/api/admin/teams/tA/members/bob", ["dbops-admin"],
            path_params={"team_id": "tA", "username": "bob"},
        ))
    assert r["statusCode"] == 200
    assert members_mock.put_item.called
    item = members_mock.put_item.call_args[1]["Item"]
    assert item["team_id"] == "tA"
    assert item["username"] == "bob"


def test_viewer_forbidden_on_member_remove():
    t = _load()
    r = t.lambda_handler(_ev(
        "DELETE", "/api/admin/teams/tA/members/bob", ["dbops-viewer"],
        path_params={"team_id": "tA", "username": "bob"},
    ))
    assert r["statusCode"] == 403


def test_admin_remove_member_deletes_row():
    t = _load()
    members_mock = MagicMock()
    with patch.object(t, "_members_table", return_value=members_mock):
        r = t.lambda_handler(_ev(
            "DELETE", "/api/admin/teams/tA/members/bob", ["dbops-admin"],
            path_params={"team_id": "tA", "username": "bob"},
        ))
    assert r["statusCode"] == 200
    assert members_mock.delete_item.called
    key = members_mock.delete_item.call_args[1]["Key"]
    assert key == {"team_id": "tA", "username": "bob"}


def test_add_member_to_nonexistent_team_404():
    t = _load()
    teams_mock = MagicMock()
    teams_mock.get_item.return_value = {}   # no Item
    members_mock = MagicMock()
    with patch.object(t, "_teams_table", return_value=teams_mock), \
         patch.object(t, "_members_table", return_value=members_mock):
        r = t.lambda_handler(_ev(
            "POST", "/api/admin/teams/ghost/members/bob", ["dbops-admin"],
            path_params={"team_id": "ghost", "username": "bob"},
        ))
    assert r["statusCode"] == 404
    assert not members_mock.put_item.called


# ---------- cluster assign / unassign ----------

def test_admin_assign_cluster_sets_team_id():
    t = _load()
    clusters = MagicMock()
    teams_mock = MagicMock()
    teams_mock.get_item.return_value = {"Item": {"team_id": "tA", "name": "A"}}
    with patch.object(t, "_clusters_table", return_value=clusters), \
         patch.object(t, "_teams_table", return_value=teams_mock):
        r = t.lambda_handler(_ev(
            "POST", "/api/admin/teams/tA/clusters/c1", ["dbops-admin"],
            path_params={"team_id": "tA", "cluster_id": "c1"},
        ))
    assert r["statusCode"] == 200
    assert clusters.update_item.called
    call_kwargs = clusters.update_item.call_args[1]
    assert call_kwargs["Key"] == {"cluster_id": "c1"}
    assert ":t" in call_kwargs["ExpressionAttributeValues"]
    assert call_kwargs["ExpressionAttributeValues"][":t"] == "tA"


def test_viewer_forbidden_on_cluster_unassign():
    t = _load()
    r = t.lambda_handler(_ev(
        "DELETE", "/api/admin/teams/tA/clusters/c1", ["dbops-viewer"],
        path_params={"team_id": "tA", "cluster_id": "c1"},
    ))
    assert r["statusCode"] == 403


def test_admin_unassign_cluster_removes_team_id():
    t = _load()
    clusters = MagicMock()
    with patch.object(t, "_clusters_table", return_value=clusters):
        r = t.lambda_handler(_ev(
            "DELETE", "/api/admin/teams/tA/clusters/c1", ["dbops-admin"],
            path_params={"team_id": "tA", "cluster_id": "c1"},
        ))
    assert r["statusCode"] == 200
    assert clusters.update_item.called
    call_kwargs = clusters.update_item.call_args[1]
    assert call_kwargs["Key"] == {"cluster_id": "c1"}
    assert "REMOVE" in call_kwargs["UpdateExpression"]


def test_assign_cluster_to_nonexistent_team_404():
    t = _load()
    clusters = MagicMock()
    teams_mock = MagicMock()
    teams_mock.get_item.return_value = {}  # no Item
    with patch.object(t, "_clusters_table", return_value=clusters), \
         patch.object(t, "_teams_table", return_value=teams_mock):
        r = t.lambda_handler(_ev(
            "POST", "/api/admin/teams/ghost/clusters/c1", ["dbops-admin"],
            path_params={"team_id": "ghost", "cluster_id": "c1"},
        ))
    assert r["statusCode"] == 404
    assert not clusters.update_item.called


# ---------- delete team — clears clusters + members ----------

def test_admin_delete_team_clears_clusters_and_members():
    t = _load()
    teams_mock = MagicMock()
    clusters_mock = MagicMock()
    members_mock = MagicMock()

    # Paginated scan: first page returns 2 clusters, no next key → done
    clusters_mock.scan.return_value = {
        "Items": [
            {"cluster_id": "c1"},
            {"cluster_id": "c2"},
        ]
    }
    # Paginated query: first page returns 2 members
    members_mock.query.return_value = {
        "Items": [
            {"team_id": "tA", "username": "alice"},
            {"team_id": "tA", "username": "bob"},
        ]
    }

    with patch.object(t, "_teams_table", return_value=teams_mock), \
         patch.object(t, "_clusters_table", return_value=clusters_mock), \
         patch.object(t, "_members_table", return_value=members_mock):
        r = t.lambda_handler(_ev(
            "DELETE", "/api/admin/teams/tA", ["dbops-admin"],
            path_params={"team_id": "tA"},
        ))

    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["deleted"] is True

    # Both clusters had team_id removed
    assert clusters_mock.update_item.call_count == 2
    updated_keys = {kw["Key"]["cluster_id"]
                    for _, kw in clusters_mock.update_item.call_args_list}
    assert updated_keys == {"c1", "c2"}

    # Both members deleted
    assert members_mock.delete_item.call_count == 2
    member_keys = {kw["Key"]["username"]
                   for _, kw in members_mock.delete_item.call_args_list}
    assert member_keys == {"alice", "bob"}

    # Team row deleted
    assert teams_mock.delete_item.called
    assert teams_mock.delete_item.call_args[1]["Key"] == {"team_id": "tA"}


def test_admin_delete_team_paginated_clusters():
    """Paginated scan: two pages of clusters, both must be cleared."""
    t = _load()
    teams_mock = MagicMock()
    clusters_mock = MagicMock()
    members_mock = MagicMock()

    # First call returns 1 cluster + LastEvaluatedKey; second call returns 1 more
    clusters_mock.scan.side_effect = [
        {"Items": [{"cluster_id": "c1"}], "LastEvaluatedKey": {"cluster_id": "c1"}},
        {"Items": [{"cluster_id": "c2"}]},
    ]
    members_mock.query.return_value = {"Items": []}

    with patch.object(t, "_teams_table", return_value=teams_mock), \
         patch.object(t, "_clusters_table", return_value=clusters_mock), \
         patch.object(t, "_members_table", return_value=members_mock):
        r = t.lambda_handler(_ev(
            "DELETE", "/api/admin/teams/tA", ["dbops-admin"],
            path_params={"team_id": "tA"},
        ))

    assert r["statusCode"] == 200
    assert clusters_mock.update_item.call_count == 2


# ---------- team detail ----------

def test_admin_get_team_detail():
    t = _load()
    teams_mock = MagicMock()
    teams_mock.get_item.return_value = {"Item": {"team_id": "tA", "name": "Alpha"}}
    members_mock = MagicMock()
    members_mock.query.return_value = {
        "Items": [{"team_id": "tA", "username": "alice"}]
    }
    clusters_mock = MagicMock()
    clusters_mock.scan.return_value = {
        "Items": [{"cluster_id": "c1"}, {"cluster_id": "c2"}]
    }

    with patch.object(t, "_teams_table", return_value=teams_mock), \
         patch.object(t, "_members_table", return_value=members_mock), \
         patch.object(t, "_clusters_table", return_value=clusters_mock):
        r = t.lambda_handler(_ev(
            "GET", "/api/admin/teams/tA", ["dbops-admin"],
            path_params={"team_id": "tA"},
        ))

    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["team_id"] == "tA"
    assert "alice" in body["members"]
    assert set(body["clusters"]) == {"c1", "c2"}


def test_get_nonexistent_team_404():
    t = _load()
    teams_mock = MagicMock()
    teams_mock.get_item.return_value = {}  # no Item
    with patch.object(t, "_teams_table", return_value=teams_mock):
        r = t.lambda_handler(_ev(
            "GET", "/api/admin/teams/ghost", ["dbops-admin"],
            path_params={"team_id": "ghost"},
        ))
    assert r["statusCode"] == 404
