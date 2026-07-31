# Admin User & Role Management (B1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin-gated console to list Cognito users and set each user's role (admin/viewer), reusing the existing two-group RBAC and the hardened canonical `_is_admin`.

**Architecture:** New `api/admin_users/` Lambda (Cognito admin APIs, IAM-scoped to the one user pool) wired into the agent stack at `GET /api/admin/users` + `POST /api/admin/users/{username}/role`; a new admin-only `/admin/users` Next.js page.

**Tech Stack:** Python 3.12 Lambda (boto3 cognito-idp), AWS CDK (Python), Next.js 16 (TypeScript).

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **Canonical fail-closed `_is_admin`** (verbatim from the admin-model-hardening branch): no-`Bearer ` → deny; empty claims → deny; non-list groups → deny; `if groups and "dbops-admin" not in groups` → deny; no-group → admin (single-admin fallback). This handler MUST use that exact form.
- **Cognito `Username` is a UUID** (= token `sub`/`cognito:username`); `email` is a display attribute only. Route `{username}` is the UUID; the self-demotion guard compares `{username}` to the caller's `cognito:username` (fallback `sub`).
- **Self-demotion guard:** demoting yourself to viewer → `409` (guarantees ≥1 admin always; no global last-admin scan).
- **No `str(e)` in error responses** — generic messages only (no internal leakage).
- **IAM scoped** to `foundation.user_pool.user_pool_arn`; Cognito actions only.
- **i18n scope:** descriptions/empty-state in Korean; role names (`admin`/`viewer`) and group names (`dbops-admin`/`dbops-viewer`) kept verbatim.
- Frontend admin gating mirrors `/settings`: nav + ⌘K entries carry `adminOnly: true`; the page surfaces a 403 as an "admin only" notice.

---

### Task 1: `api/admin_users/handler.py` + unit tests

**Files:**

- Create: `api/admin_users/handler.py`
- Create: `tests/unit/api/test_admin_users.py`

**Interfaces:**

- Produces: HTTP handler `lambda_handler(event, context=None)` for `GET /api/admin/users` and `POST /api/admin/users/{username}/role`. Env: `USER_POOL_ID`. Helpers `_is_admin`, `_caller_username`, `_role_for_groups`, `_list_users`, `_set_role`, `_client` (returns a `boto3.client("cognito-idp")`).

- [ ] **Step 1: Write the failing tests.** Create `tests/unit/api/test_admin_users.py`:

```python
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
```

- [ ] **Step 2: Run the tests to confirm they fail** (no handler yet).

Run: `python -m pytest tests/unit/api/test_admin_users.py -q`
Expected: collection/import error or failures (module `api/admin_users/handler.py` does not exist).

- [ ] **Step 3: Write `api/admin_users/handler.py`:**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass.**

Run: `python -m pytest tests/unit/api/test_admin_users.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit.**

```bash
git add api/admin_users/handler.py tests/unit/api/test_admin_users.py
git commit -m "feat(admin-console): admin user/role management API (list users + set role, self-demotion guarded)"
```

---

### Task 2: CDK wiring — Lambda + IAM + routes

**Files:**

- Modify: `cdk/stacks/agent_stack.py` (add the Lambda, IAM policy, and two routes — place near the existing `onboarding_lambda` block, around the App config / Onboarding route section)

**Interfaces:**

- Consumes: `foundation.user_pool` (cognito.UserPool — has `.user_pool_id` and `.user_pool_arn`), `self.api` (HttpApi), `integrations`, `apigwv2`, `iam`, `lambda_`, `cdk` (all already imported in this file).
- Produces: routes `GET /api/admin/users` and `POST /api/admin/users/{username}/role` behind the existing Cognito JWT authorizer.

- [ ] **Step 1: Add the Lambda + IAM + routes.** In `cdk/stacks/agent_stack.py`, immediately AFTER the `onboarding_lambda` route registration block (the `self.api.add_routes(path="/api/onboarding/template", ...)` call), insert:

```python
        # Admin console — Cognito user & role management (admin-gated)
        admin_users_lambda = lambda_.Function(
            self, "AdminUsersApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/admin_users"),
            timeout=cdk.Duration.seconds(15),
            environment={"USER_POOL_ID": foundation.user_pool.user_pool_id},
        )
        admin_users_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "cognito-idp:ListUsers",
                "cognito-idp:AdminListGroupsForUser",
                "cognito-idp:AdminAddUserToGroup",
                "cognito-idp:AdminRemoveUserFromGroup",
            ],
            resources=[foundation.user_pool.user_pool_arn],
        ))
        admin_users_integration = integrations.HttpLambdaIntegration(
            "AdminUsersIntegration", admin_users_lambda)
        self.api.add_routes(
            path="/api/admin/users",
            methods=[apigwv2.HttpMethod.GET],
            integration=admin_users_integration,
        )
        self.api.add_routes(
            path="/api/admin/users/{username}/role",
            methods=[apigwv2.HttpMethod.POST],
            integration=admin_users_integration,
        )
```

- [ ] **Step 2: Verify imports exist.** Confirm `iam`, `lambda_`, `integrations`, `apigwv2`, `cdk` are already imported at the top of `agent_stack.py` (they are — used by neighboring blocks like `onboarding_lambda`). If `foundation.user_pool` has no `user_pool_arn` attribute, it does (CDK `cognito.UserPool` exposes `.user_pool_arn`).

- [ ] **Step 3: Run the synth smoke test.**

Run: `python -m pytest tests/cdk/test_synth.py -q`
Expected: PASS (synth succeeds, 4 stacks present). This test is a structural smoke test, not a frozen snapshot — adding a Lambda + routes does not break it.

- [ ] **Step 4: Commit.**

```bash
git add cdk/stacks/agent_stack.py
git commit -m "feat(admin-console): wire admin-users Lambda + Cognito IAM + routes in agent stack"
```

---

### Task 3: Frontend — `/admin/users` page + api-client + auth helper + nav/⌘K

**Files:**

- Modify: `frontend/src/lib/auth.ts` (add `getUsername()`)
- Modify: `frontend/src/lib/api-client.ts` (add `AdminUser` type + `fetchAdminUsers` + `updateUserRole`)
- Create: `frontend/src/app/admin/users/page.tsx`
- Modify: `frontend/src/components/app-shell.tsx` (add nav entry, `adminOnly: true`)
- Modify: `frontend/src/components/design-system/command-palette.tsx` (add ⌘K entry, `adminOnly: true`)

**Interfaces:**

- Consumes: `authedFetch`, `apiUrl`, `enc` (api-client); `decodeJwt`, `getToken` (auth.ts — already used by `getUserGroups`); `isAdmin` (auth.ts); design-system `PageBody/PageHeader/Section/EmptyState`.
- Produces: an admin-only page at `/admin/users`.

- [ ] **Step 1: Add `getUsername()` to `frontend/src/lib/auth.ts`.** After `getUserGroups()` (which uses `decodeJwt(getToken())`), add:

```typescript
// The pool's Cognito username is a UUID (== sub); email is a display
// attribute. Used to disable the acting admin's own role control.
export function getUsername(): string | null {
  const claims = decodeJwt(getToken()) as {
    "cognito:username"?: string;
    sub?: string;
  } | null;
  return claims?.["cognito:username"] || claims?.sub || null;
}
```

- [ ] **Step 2: Add the API client functions to `frontend/src/lib/api-client.ts`** (near `fetchAppConfig`, end of file). Mirror the `fetchAppConfig` 403→"admin only" pattern:

```typescript
// =====  Admin user/role management (admin-gated)  =====

export interface AdminUser {
  username: string;
  email: string | null;
  status: string | null;
  enabled: boolean;
  created: string | null;
  role: "admin" | "viewer";
  implicit: boolean;
}

export async function fetchAdminUsers(
  cursor?: string,
): Promise<{ items: AdminUser[]; next_cursor: string | null }> {
  const path = cursor
    ? `/api/admin/users?cursor=${enc(cursor)}`
    : "/api/admin/users";
  const res = await authedFetch(await apiUrl(path));
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`사용자 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function updateUserRole(
  username: string,
  role: "admin" | "viewer",
): Promise<{ username: string; role: string }> {
  const res = await authedFetch(
    await apiUrl(`/api/admin/users/${enc(username)}/role`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    },
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) {
    let msg = `역할 변경 실패 (상태 ${res.status})`;
    try {
      const b = await res.json();
      if (b?.error) msg = b.error;
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }
  return res.json();
}
```

- [ ] **Step 3: Create `frontend/src/app/admin/users/page.tsx`:**

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchAdminUsers,
  updateUserRole,
  type AdminUser,
} from "@/lib/api-client";
import { getUsername } from "@/lib/auth";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";

export default function AdminUsersPage() {
  const [items, setItems] = useState<AdminUser[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [adminOnly, setAdminOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [me, setMe] = useState<string | null>(null);

  useEffect(() => {
    setMe(getUsername());
  }, []);

  const load = useCallback((cursor?: string) => {
    setLoading(true);
    setError(null);
    if (!cursor) setAdminOnly(false);
    fetchAdminUsers(cursor)
      .then((d) => {
        setItems((prev) => (cursor ? [...prev, ...d.items] : d.items));
        setNextCursor(d.next_cursor);
      })
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e);
        if (msg === "admin only") setAdminOnly(true);
        else setError(msg);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onChangeRole = async (u: AdminUser, role: "admin" | "viewer") => {
    if (role === u.role) return;
    const label = u.email || u.username;
    if (
      !window.confirm(
        `${label} 사용자의 역할을 '${role}'(으)로 변경하시겠습니까?`,
      )
    )
      return;
    setBusy(u.username);
    setError(null);
    try {
      await updateUserRole(u.username, role);
      setItems((prev) =>
        prev.map((it) =>
          it.username === u.username ? { ...it, role, implicit: false } : it,
        ),
      );
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  if (!loading && adminOnly) {
    return (
      <PageBody>
        <PageHeader
          eyebrow="Admin"
          title="Users"
          description="사용자 역할 관리 (관리자 전용)"
        />
        <Section>
          <EmptyState
            eyebrow="접근 제한"
            title="관리자 전용 페이지"
            description="이 페이지는 관리자만 볼 수 있습니다."
          />
        </Section>
      </PageBody>
    );
  }

  return (
    <PageBody>
      <PageHeader
        eyebrow="Admin"
        title="Users"
        description="사용자 목록과 역할(admin · viewer)을 관리합니다. 변경 사항은 즉시 적용됩니다."
      />

      {error && (
        <div className="mb-6 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {error}
        </div>
      )}

      {loading && items.length === 0 ? (
        <div className="text-sm text-zinc-500">불러오는 중…</div>
      ) : items.length === 0 ? (
        <Section>
          <EmptyState
            eyebrow="비어 있음"
            title="사용자가 없습니다"
            description="이 사용자 풀에 등록된 사용자가 없습니다."
          />
        </Section>
      ) : (
        <Section eyebrow="Identity" title="사용자">
          <div className="border border-zinc-800 bg-zinc-900/30">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-zinc-500 border-b border-zinc-800">
                  <th className="px-4 py-2.5 font-medium">Email</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5 font-medium">Role</th>
                  <th className="px-4 py-2.5 font-medium text-right">변경</th>
                </tr>
              </thead>
              <tbody>
                {items.map((u) => {
                  const isSelf = me != null && u.username === me;
                  return (
                    <tr
                      key={u.username}
                      className="border-b border-zinc-800/60 last:border-0"
                    >
                      <td className="px-4 py-3 text-zinc-200">
                        {u.email || (
                          <span className="font-mono text-zinc-500">
                            {u.username}
                          </span>
                        )}
                        {isSelf && (
                          <span className="ml-2 text-[10px] text-emerald-400/80">
                            (나)
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-zinc-400">
                        {u.status}
                        {!u.enabled && (
                          <span className="ml-1 text-rose-400">· 비활성</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={
                            u.role === "admin"
                              ? "text-emerald-300"
                              : "text-zinc-300"
                          }
                        >
                          {u.role}
                        </span>
                        {u.implicit && (
                          <span className="ml-2 text-[10px] text-zinc-500">
                            (암묵 — 명시 역할 미지정)
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <select
                          value={u.role}
                          disabled={isSelf || busy === u.username}
                          title={
                            isSelf
                              ? "자신의 역할은 변경할 수 없습니다"
                              : undefined
                          }
                          onChange={(e) =>
                            onChangeRole(
                              u,
                              e.target.value as "admin" | "viewer",
                            )
                          }
                          className="bg-zinc-800 border border-zinc-700 text-zinc-100 text-xs px-2 py-1 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                          <option value="admin">admin</option>
                          <option value="viewer">viewer</option>
                        </select>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {nextCursor && (
            <button
              type="button"
              onClick={() => load(nextCursor)}
              disabled={loading}
              className="mt-4 text-xs text-zinc-400 hover:text-zinc-200 border border-zinc-700 px-3 py-1.5 rounded disabled:opacity-40"
            >
              더 불러오기
            </button>
          )}
        </Section>
      )}
    </PageBody>
  );
}
```

- [ ] **Step 4: Add the nav entry in `frontend/src/components/app-shell.tsx`.** In the same nav group as `/settings` / `/onboarding` (the admin items), add an entry. Use an icon already imported in the file (e.g. `Users` if imported; otherwise reuse `UserCheck` which is already imported for Approval policies):

```tsx
      {
        href: "/admin/users",
        label: "Users",
        icon: UserCheck,
        adminOnly: true,
        hint: "사용자 역할 관리 — admin · viewer (관리자)",
      },
```

(Place it adjacent to the other `adminOnly: true` items. Do NOT add a new icon import unless needed — reuse `UserCheck`.)

- [ ] **Step 5: Add the ⌘K entry in `frontend/src/components/design-system/command-palette.tsx`.** In the `commands` array, near the other `adminOnly: true` Configure entries, add:

```tsx
  {
    id: "admin-users",
    label: "Users — 사용자 역할 관리",
    path: "/admin/users",
    group: "Configure",
    adminOnly: true,
  },
```

- [ ] **Step 6: Build.**

Run: `cd frontend && npm run build`
Expected: PASS, no type errors, `/admin/users` appears in the prerendered route list.

- [ ] **Step 7: Commit.**

```bash
git add frontend/src/lib/auth.ts frontend/src/lib/api-client.ts frontend/src/app/admin/users/page.tsx frontend/src/components/app-shell.tsx frontend/src/components/design-system/command-palette.tsx
git commit -m "feat(admin-console): /admin/users page + nav/⌘K + api-client (admin role management UI)"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD` — focus: the handler uses the canonical fail-closed `_is_admin`; self-demotion guard is correct (compares to caller `cognito:username`/`sub`); no `str(e)` leakage; IAM scoped to the one pool ARN; role-set is exclusive (add one group + remove the other); frontend gating mirrors `/settings`.
- Deploy dev: `cdk deploy dbops-dev-agent` (new Lambda + IAM + routes). Frontend build → `aws s3 sync frontend/out/ s3://dbops-dev-frontend-123456789012 --delete --exclude config.json --region ap-northeast-2` → CloudFront invalidation `E1234567890ABC`.
- Live smoke (viewer e2e token): `GET /api/admin/users` with `Bearer` viewer → **403**; with raw scheme-less token → **403**; (admin happy-path GET/POST not reachable with the viewer-only e2e token — unit-covered). Confirm the new route exists (not 404-route).
- Then `superpowers:finishing-a-development-branch`.
