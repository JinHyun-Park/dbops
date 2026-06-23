"""Tests for the admin user/role management handler."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "admin_users" / "handler.py"
_spec = importlib.util.spec_from_file_location("admin_users_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

CALLER_SUB = "caller-uuid-123"


def _jwt(groups=("dbops-admin",), sub=CALLER_SUB) -> str:
    payload = {"sub": sub, "cognito:username": sub, "email": "a@b.c"}
    if groups is not None:
        payload["cognito:groups"] = list(groups)
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, path_params=None, body=None, groups=("dbops-admin",), qs=None, bearer=True):
    e = {
        "requestContext": {"http": {"method": method}},
        "headers": {},
        "pathParameters": path_params or {},
        "queryStringParameters": qs,
    }
    if bearer:
        e["headers"]["authorization"] = f"Bearer {_jwt(groups=groups)}"
    if body is not None:
        e["body"] = json.dumps(body)
    return e


def _fake_client(users=None, groups_by_user=None):
    c = MagicMock()
    c.list_users.return_value = {
        "Users": users if users is not None else [],
        "PaginationToken": "next-tok",
    }
    gmap = groups_by_user or {}
    c.admin_list_groups_for_user.side_effect = lambda UserPoolId, Username: {
        "Groups": [{"GroupName": g} for g in gmap.get(Username, [])]
    }
    return c


def test_get_lists_users_with_roles():
    users = [
        {"Username": "u-admin", "UserStatus": "CONFIRMED", "Enabled": True,
         "Attributes": [{"Name": "email", "Value": "admin@x"}]},
        {"Username": "u-viewer", "UserStatus": "CONFIRMED", "Enabled": True,
         "Attributes": [{"Name": "email", "Value": "viewer@x"}]},
        {"Username": "u-none", "UserStatus": "CONFIRMED", "Enabled": True,
         "Attributes": [{"Name": "email", "Value": "none@x"}]},
    ]
    gmap = {"u-admin": ["dbops-admin"], "u-viewer": ["dbops-viewer"], "u-none": []}
    fake = _fake_client(users, gmap)
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            r = handler.lambda_handler(_event("GET", path_params={}))
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    by_name = {i["username"]: i for i in body["items"]}
    assert by_name["u-admin"]["role"] == "admin" and by_name["u-admin"]["implicit"] is False
    assert by_name["u-viewer"]["role"] == "viewer"
    assert by_name["u-none"]["role"] == "admin" and by_name["u-none"]["implicit"] is True
    assert by_name["u-admin"]["email"] == "admin@x"
    assert body["next_cursor"] == "next-tok"


def test_get_passes_cursor():
    fake = _fake_client([], {})
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            handler.lambda_handler(_event("GET", qs={"cursor": "abc"}))
    _, kwargs = fake.list_users.call_args
    assert kwargs.get("PaginationToken") == "abc"


def test_post_role_admin_adds_admin_removes_viewer():
    fake = _fake_client()
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            r = handler.lambda_handler(
                _event("POST", path_params={"username": "u-target"}, body={"role": "admin"}))
    assert r["statusCode"] == 200
    fake.admin_add_user_to_group.assert_called_once_with(
        UserPoolId="pool-1", Username="u-target", GroupName="dbops-admin")
    fake.admin_remove_user_from_group.assert_called_once_with(
        UserPoolId="pool-1", Username="u-target", GroupName="dbops-viewer")


def test_post_role_viewer_adds_viewer_removes_admin():
    fake = _fake_client()
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            r = handler.lambda_handler(
                _event("POST", path_params={"username": "u-other"}, body={"role": "viewer"}))
    assert r["statusCode"] == 200
    fake.admin_add_user_to_group.assert_called_once_with(
        UserPoolId="pool-1", Username="u-other", GroupName="dbops-viewer")
    fake.admin_remove_user_from_group.assert_called_once_with(
        UserPoolId="pool-1", Username="u-other", GroupName="dbops-admin")


def test_post_self_demotion_blocked_409_no_write():
    fake = _fake_client()
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            # route username == caller sub → demote self to viewer → 409
            r = handler.lambda_handler(
                _event("POST", path_params={"username": CALLER_SUB}, body={"role": "viewer"}))
    assert r["statusCode"] == 409
    fake.admin_add_user_to_group.assert_not_called()
    fake.admin_remove_user_from_group.assert_not_called()


def test_post_self_promote_to_admin_allowed():
    # Setting your OWN role to admin is fine (no lockout risk).
    fake = _fake_client()
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            r = handler.lambda_handler(
                _event("POST", path_params={"username": CALLER_SUB}, body={"role": "admin"}))
    assert r["statusCode"] == 200


def test_post_bad_role_400():
    fake = _fake_client()
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            r = handler.lambda_handler(
                _event("POST", path_params={"username": "u-x"}, body={"role": "superuser"}))
    assert r["statusCode"] == 400
    fake.admin_add_user_to_group.assert_not_called()


def test_post_malformed_body_400():
    fake = _fake_client()
    e = _event("POST", path_params={"username": "u-x"})
    e["body"] = "{not json"
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            r = handler.lambda_handler(e)
    assert r["statusCode"] == 400


def test_post_user_not_found_404():
    fake = _fake_client()
    fake.admin_add_user_to_group.side_effect = ClientError(
        {"Error": {"Code": "UserNotFoundException", "Message": "x"}}, "AdminAddUserToGroup")
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            r = handler.lambda_handler(
                _event("POST", path_params={"username": "ghost"}, body={"role": "admin"}))
    assert r["statusCode"] == 404


def test_post_other_cognito_error_500_generic():
    fake = _fake_client()
    fake.admin_add_user_to_group.side_effect = ClientError(
        {"Error": {"Code": "TooManyRequestsException", "Message": "secret-internal-detail"}},
        "AdminAddUserToGroup")
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            r = handler.lambda_handler(
                _event("POST", path_params={"username": "u-x"}, body={"role": "admin"}))
    assert r["statusCode"] == 500
    assert "secret-internal-detail" not in r["body"]


def test_options_bypasses_auth():
    r = handler.lambda_handler(_event("OPTIONS", bearer=False))
    assert r["statusCode"] == 200


# --- admin-gate contract (canonical fail-closed) ---

def test_no_bearer_denied():
    r = handler.lambda_handler(_event("GET", bearer=False))
    assert r["statusCode"] == 403


def test_raw_token_no_bearer_denied():
    e = _event("GET", bearer=False)
    e["headers"]["authorization"] = _jwt(groups=("dbops-admin",))  # raw, no "Bearer "
    r = handler.lambda_handler(e)
    assert r["statusCode"] == 403


def test_bearer_garbage_denied():
    e = _event("GET", bearer=False)
    e["headers"]["authorization"] = "Bearer not-a-jwt"
    r = handler.lambda_handler(e)
    assert r["statusCode"] == 403


def test_viewer_denied():
    r = handler.lambda_handler(_event("GET", groups=("dbops-viewer",)))
    assert r["statusCode"] == 403


def test_analyst_group_denied():
    r = handler.lambda_handler(_event("GET", groups=("dbops-analyst",)))
    assert r["statusCode"] == 403


def test_no_group_is_admin():
    fake = _fake_client([], {})
    with patch.object(handler, "_client", return_value=fake):
        with patch.dict("os.environ", {"USER_POOL_ID": "pool-1"}):
            r = handler.lambda_handler(_event("GET", groups=None))
    assert r["statusCode"] != 403
