"""Admin user & role management API (admin-gated).

Routes:
  GET  /api/admin/users                   — list Cognito users + derived role
  POST /api/admin/users/{username}/role   — set a user's role (admin|viewer)

The pool's Cognito Username is a UUID (== token sub / cognito:username); email
is a display attribute. The self-demotion guard compares {username} to the
caller's cognito:username (fallback sub), which guarantees the acting admin
stays admin (the pool can never be driven to zero admins via this API).
"""

import base64
import json
import os

import boto3
from botocore.exceptions import ClientError

ADMIN_GROUP = "dbops-admin"
VIEWER_GROUP = "dbops-viewer"


def _client():
    return boto3.client("cognito-idp")


def _pool() -> str:
    return os.environ["USER_POOL_ID"]


# --- auth helpers (canonical fail-closed, mirror api/config/handler.py) ---


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _claims(event: dict) -> dict:
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return {}
    return _decode_jwt_payload(auth.split(" ", 1)[1])


def _caller_username(event: dict) -> str:
    c = _claims(event)
    return c.get("cognito:username") or c.get("sub") or ""


def _is_admin(event: dict) -> bool:
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and ADMIN_GROUP not in groups:
        return False
    return True


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _role_for_groups(groups: list):
    """Return (role, implicit). admin if in admin group OR no groups at all
    (single-admin dev fallback, matching the canonical _is_admin)."""
    if ADMIN_GROUP in groups:
        return "admin", False
    if not groups:
        return "admin", True
    return "viewer", False


def _list_users(cursor=None) -> dict:
    cli = _client()
    pool = _pool()
    kwargs = {"UserPoolId": pool, "Limit": 60}
    if cursor:
        kwargs["PaginationToken"] = cursor
    resp = cli.list_users(**kwargs)
    items = []
    for u in resp.get("Users", []):
        username = u.get("Username")
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        g = cli.admin_list_groups_for_user(UserPoolId=pool, Username=username)
        gnames = [x.get("GroupName") for x in g.get("Groups", [])]
        role, implicit = _role_for_groups(gnames)
        items.append({
            "username": username,
            "email": attrs.get("email"),
            "status": u.get("UserStatus"),
            "enabled": u.get("Enabled", True),
            "created": u.get("UserCreateDate"),
            "role": role,
            "implicit": implicit,
        })
    return {"items": items, "next_cursor": resp.get("PaginationToken")}


def _set_role(username: str, role: str):
    cli = _client()
    pool = _pool()
    if role == "admin":
        cli.admin_add_user_to_group(UserPoolId=pool, Username=username, GroupName=ADMIN_GROUP)
        cli.admin_remove_user_from_group(UserPoolId=pool, Username=username, GroupName=VIEWER_GROUP)
    else:
        cli.admin_add_user_to_group(UserPoolId=pool, Username=username, GroupName=VIEWER_GROUP)
        cli.admin_remove_user_from_group(UserPoolId=pool, Username=username, GroupName=ADMIN_GROUP)


def lambda_handler(event, context=None):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()

    if method == "OPTIONS":
        return _resp(200, {})

    if not _is_admin(event):
        return _resp(403, {"error": "admin only"})

    path_params = event.get("pathParameters") or {}

    if method == "GET":
        qs = event.get("queryStringParameters") or {}
        cursor = qs.get("cursor")
        try:
            return _resp(200, _list_users(cursor))
        except ClientError:
            return _resp(500, {"error": "failed to list users"})

    if method == "POST":
        username = path_params.get("username")
        if not username:
            return _resp(404, {"error": "not found"})
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "malformed JSON body"})
        role = body.get("role")
        if role not in ("admin", "viewer"):
            return _resp(400, {"error": "role must be 'admin' or 'viewer'"})
        if role == "viewer" and username == _caller_username(event):
            return _resp(409, {"error": "cannot remove your own admin role"})
        try:
            _set_role(username, role)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "UserNotFoundException":
                return _resp(404, {"error": "user not found"})
            return _resp(500, {"error": "failed to set role"})
        return _resp(200, {"username": username, "role": role})

    return _resp(405, {"error": f"method {method} not allowed"})
