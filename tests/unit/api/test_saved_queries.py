"""Tests for the saved_queries API handler."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = (
    Path(__file__).resolve().parents[3] / "api" / "saved_queries" / "handler.py"
)
_spec = importlib.util.spec_from_file_location("saved_queries_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _jwt(user="alice", admin=False) -> str:
    payload = {
        "preferred_username": user,
        "cognito:groups": ["dbops-admin"] if admin else ["dbops-viewer"],
    }
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, body=None, path_params=None, qs=None, user="alice", admin=True):
    # P2.4.2: mutating routes are admin-gated, so the default caller is an admin.
    # Viewer-denial cases pass admin=False explicitly (see the DELETE viewer test).
    e = {
        "httpMethod": method,
        "requestContext": {"http": {"method": method}},
        "pathParameters": path_params or {},
        "queryStringParameters": qs or {},
        "headers": {"authorization": f"Bearer {_jwt(user, admin)}"},
    }
    if body is not None:
        e["body"] = json.dumps(body) if not isinstance(body, str) else body
    return e


@patch.object(handler, "_execute")
def test_list_no_filters(mock_execute):
    mock_execute.return_value = [
        {"id": 1, "cluster_id": None, "title": "Capacity probe", "tags": ["capacity"]},
    ]
    res = handler.lambda_handler(_event("GET"), None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["queries"][0]["title"] == "Capacity probe"
    # No WHERE clause when no filters set.
    call_sql = mock_execute.call_args.args[0]
    assert "WHERE" not in call_sql


@patch.object(handler, "_execute")
def test_list_cluster_filter(mock_execute):
    mock_execute.return_value = []
    handler.lambda_handler(
        _event("GET", qs={"cluster_id": "prod-pg-1"}), None,
    )
    call_sql = mock_execute.call_args.args[0]
    call_params = mock_execute.call_args.args[1]
    assert "cluster_id = :cid" in call_sql
    assert call_params["cid"] == "prod-pg-1"


@patch.object(handler, "_execute")
def test_list_tag_filter(mock_execute):
    mock_execute.return_value = []
    handler.lambda_handler(_event("GET", qs={"tag": "audit"}), None)
    call_sql = mock_execute.call_args.args[0]
    assert ":tag = ANY(tags)" in call_sql


@patch.object(handler, "_execute")
def test_create_happy_path(mock_execute):
    mock_execute.return_value = [
        {"id": 1, "title": "Test", "sql_text": "SELECT 1", "tags": []}
    ]
    res = handler.lambda_handler(
        _event("POST", body={"title": "Test", "sql_text": "SELECT 1"}),
        None,
    )
    assert res["statusCode"] == 201
    body = json.loads(res["body"])
    assert body["title"] == "Test"


def test_create_missing_title():
    res = handler.lambda_handler(
        _event("POST", body={"sql_text": "SELECT 1"}),
        None,
    )
    assert res["statusCode"] == 400
    assert "title required" in json.loads(res["body"])["error"]


def test_create_missing_sql():
    res = handler.lambda_handler(
        _event("POST", body={"title": "T"}),
        None,
    )
    assert res["statusCode"] == 400
    assert "sql_text required" in json.loads(res["body"])["error"]


def test_create_too_many_tags():
    res = handler.lambda_handler(
        _event(
            "POST",
            body={
                "title": "T",
                "sql_text": "SELECT 1",
                "tags": ["a"] * 20,
            },
        ),
        None,
    )
    assert res["statusCode"] == 400
    assert "too many tags" in json.loads(res["body"])["error"]


def test_create_bad_cluster_id():
    res = handler.lambda_handler(
        _event(
            "POST",
            body={
                "title": "T",
                "sql_text": "SELECT 1",
                "cluster_id": "drop table users; --",
            },
        ),
        None,
    )
    assert res["statusCode"] == 400
    assert "invalid cluster_id" in json.loads(res["body"])["error"]


@patch.object(handler, "_execute")
def test_create_strips_blank_tags(mock_execute):
    mock_execute.return_value = [{"id": 1}]
    handler.lambda_handler(
        _event(
            "POST",
            body={
                "title": "T",
                "sql_text": "SELECT 1",
                "tags": ["audit", "", "  ", "capacity"],
            },
        ),
        None,
    )
    # Blank tags filtered out before joining.
    params = mock_execute.call_args.args[1]
    assert params["tags_csv"] == "audit,capacity"


@patch.object(handler, "_execute")
def test_get_one_returns_full_detail(mock_execute):
    mock_execute.return_value = [
        {"id": 5, "title": "T", "sql_text": "SELECT 1", "tags": []}
    ]
    res = handler.lambda_handler(
        _event("GET", path_params={"id": "5"}), None,
    )
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["sql_text"] == "SELECT 1"


@patch.object(handler, "_execute")
def test_get_one_404_when_missing(mock_execute):
    mock_execute.return_value = []
    res = handler.lambda_handler(
        _event("GET", path_params={"id": "999"}), None,
    )
    assert res["statusCode"] == 404


def test_get_one_invalid_id():
    res = handler.lambda_handler(
        _event("GET", path_params={"id": "abc"}), None,
    )
    assert res["statusCode"] == 400


@patch.object(handler, "_execute")
def test_delete_admin(mock_execute):
    mock_execute.return_value = [{"id": 3}]
    res = handler.lambda_handler(
        _event("DELETE", path_params={"id": "3"}, admin=True), None,
    )
    assert res["statusCode"] == 200


def test_delete_viewer_blocked():
    res = handler.lambda_handler(
        _event("DELETE", path_params={"id": "3"}, user="bob", admin=False),
        None,
    )
    assert res["statusCode"] == 403


@patch.object(handler, "_execute")
def test_update_admin(mock_execute):
    mock_execute.return_value = [
        {"id": 7, "title": "Updated", "sql_text": "SELECT 2", "tags": []}
    ]
    res = handler.lambda_handler(
        _event(
            "PUT",
            path_params={"id": "7"},
            body={"title": "Updated", "sql_text": "SELECT 2"},
            admin=True,
        ),
        None,
    )
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["title"] == "Updated"


def test_update_viewer_blocked():
    res = handler.lambda_handler(
        _event(
            "PUT",
            path_params={"id": "7"},
            body={"title": "Updated", "sql_text": "SELECT 2"},
            admin=False,
        ),
        None,
    )
    assert res["statusCode"] == 403


def test_unknown_method():
    res = handler.lambda_handler(_event("PATCH", path_params={"id": "1"}), None)
    assert res["statusCode"] == 405


# ---------------------------------------------------------------------------
# Admin-gate contract tests (regression for priv-esc via no-bearer / unknown-group)
# ---------------------------------------------------------------------------

def _raw_event(method, auth_header, path_params=None, body=None):
    """Build an event with an explicit auth header value (used for boundary cases)."""
    e = {
        "httpMethod": method,
        "requestContext": {"http": {"method": method}},
        "pathParameters": path_params or {},
        "queryStringParameters": {},
        "headers": {"authorization": auth_header} if auth_header else {},
    }
    if body is not None:
        e["body"] = json.dumps(body)
    return e


def _jwt_with_groups(groups) -> str:
    """Build a JWT whose cognito:groups is exactly `groups` (list or absent)."""
    payload = {"preferred_username": "tester"}
    if groups is not None:
        payload["cognito:groups"] = groups
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def test_put_no_bearer_prefix_denied():
    # Raw token without "Bearer " prefix was the priv-esc: _caller_groups returned []
    # which the old code treated as admin. Must now be 403.
    raw_token = _jwt_with_groups(["dbops-admin"])  # valid admin token, but no "Bearer "
    res = handler.lambda_handler(
        _raw_event("PUT", raw_token, path_params={"id": "7"},
                   body={"title": "T", "sql_text": "SELECT 1"}),
        None,
    )
    assert res["statusCode"] == 403


def test_put_garbage_token_denied():
    # "Bearer <non-jwt>" decodes to empty claims — must NOT be treated as dev-fallback admin.
    res = handler.lambda_handler(
        _raw_event("PUT", "Bearer not-a-real-jwt", path_params={"id": "7"},
                   body={"title": "T", "sql_text": "SELECT 1"}),
        None,
    )
    assert res["statusCode"] == 403


def test_put_viewer_group_denied():
    res = handler.lambda_handler(
        _raw_event("PUT", f"Bearer {_jwt_with_groups(['dbops-viewer'])}",
                   path_params={"id": "7"}, body={"title": "T", "sql_text": "SELECT 1"}),
        None,
    )
    assert res["statusCode"] == 403


def test_put_analyst_group_denied():
    # Unknown/other group without dbops-admin → deny (closes unknown-group hole).
    res = handler.lambda_handler(
        _raw_event("PUT", f"Bearer {_jwt_with_groups(['dbops-analyst'])}",
                   path_params={"id": "7"}, body={"title": "T", "sql_text": "SELECT 1"}),
        None,
    )
    assert res["statusCode"] == 403


def test_delete_no_bearer_prefix_denied():
    raw_token = _jwt_with_groups(["dbops-admin"])
    res = handler.lambda_handler(
        _raw_event("DELETE", raw_token, path_params={"id": "3"}),
        None,
    )
    assert res["statusCode"] == 403


@patch.object(handler, "_execute")
def test_put_no_groups_claim_is_admin(mock_execute):
    # Valid token with NO cognito:groups claim = one-admin dev fallback → admin.
    mock_execute.return_value = [
        {"id": 7, "title": "T", "sql_text": "SELECT 1", "tags": []}
    ]
    res = handler.lambda_handler(
        _raw_event("PUT", f"Bearer {_jwt_with_groups(None)}",
                   path_params={"id": "7"}, body={"title": "T", "sql_text": "SELECT 1"}),
        None,
    )
    assert res["statusCode"] != 403


@patch.object(handler, "_execute")
def test_put_admin_group_allowed(mock_execute):
    mock_execute.return_value = [
        {"id": 7, "title": "T", "sql_text": "SELECT 1", "tags": []}
    ]
    res = handler.lambda_handler(
        _raw_event("PUT", f"Bearer {_jwt_with_groups(['dbops-admin'])}",
                   path_params={"id": "7"}, body={"title": "T", "sql_text": "SELECT 1"}),
        None,
    )
    assert res["statusCode"] != 403
