# DBOps Admin Console — User & Role Management (B1) — Design

**Date:** 2026-06-24
**Status:** approved (autonomous — design decisions made by the implementer per the user's "proceed without asking" directive)

## Context & Decomposition

The backlog item "DBOps Admin 관리 기능 (admin console + 멀티팀 접근/공유)" spans two
independent subsystems. Per the brainstorming scope-check, it is decomposed:

- **B1 (this spec) — Admin User & Role Management.** A console where an admin
  lists the Cognito users of the pool and sets each user's role (admin /
  viewer). Self-contained: reuses the EXISTING two-group RBAC
  (`dbops-admin` / `dbops-viewer`) and the just-hardened canonical `_is_admin`.
  No new tenancy architecture. Today an operator can only manage who is
  admin/viewer through the raw AWS Cognito console — this closes that gap
  inside DBOps.
- **B2 (DEFERRED — needs user architectural direction) — Multi-team tenancy +
  team-scoped cluster/view sharing.** This is product-architecture-defining:
  the team model shape, default cluster visibility (open vs closed), whether
  teams map to Cognito-group-per-team vs a DB membership table, integration
  with the existing fleet grouping + saved views, and cross-account team
  mapping. Like the Cedar ENFORCE flip, this is a decision the user owns; it
  stays a flagged backlog item rather than being settled autonomously.

This spec covers **B1 only**.

## Problem

There is no in-product way to see who the platform's users are or to change a
user's role. Promoting/demoting requires the AWS Cognito console. Admins
managing a fleet team need a first-class console.

## Goal

An admin-gated `GET /api/admin/users` (list users + derived role) and
`POST /api/admin/users/{username}/role` (set role to admin or viewer), plus an
admin-only `/admin/users` UI page. Reuse the existing groups and gate; add no
new identity model.

Non-goals (B1): creating or deleting users, password resets, MFA management,
inviting users, any team/multi-tenancy concept (that is B2), editing
attributes other than group membership.

## Identity model facts (verified against the live dev pool)

- The pool's Cognito **`Username` is a UUID** (equals the token `sub` and
  `cognito:username`); `preferred_username` is absent. The **email** is a
  user attribute, used for display only.
- Therefore the API's `{username}` path segment is the UUID, `ListUsers`
  returns that UUID as `Username`, and the **self-demotion guard compares the
  `{username}` to the caller's `cognito:username` (fallback `sub`)** decoded
  from the bearer token — NOT the email.
- Two groups exist (CDK-managed `CfnUserPoolGroup`): `dbops-admin`,
  `dbops-viewer`. Role derivation:
  - in `dbops-admin` → **admin**
  - in `dbops-viewer` and not `dbops-admin` → **viewer**
  - in NO group → **admin (implicit)** — this matches the canonical
    `_is_admin` single-admin dev fallback; surfaced with an `implicit: true`
    flag so the UI can show "implicit admin — assign an explicit role".

## Architecture

### Component 1 — `api/admin_users/handler.py` (new Lambda, agent stack)

Mirrors `api/config/handler.py` structure: its own exception-safe
`_decode_jwt_payload` + the **canonical fail-closed `_is_admin`** (verbatim from
the admin-model-hardening canonical form), `_resp`, and a `lambda_handler`
that routes on method + path.

Routes (the HTTP API's Cognito JWT authorizer already fronts every route):

- `OPTIONS *` → `200` (CORS preflight, before the auth gate).
- `GET /api/admin/users` (admin-only):
  - `cognito-idp list_users(UserPoolId, Limit=60, PaginationToken=cursor?)`.
  - For each user, `admin_list_groups_for_user(Username, UserPoolId)` →
    derive role + `implicit`.
  - Returns `{"items": [{username, email, status, enabled, created, role,
implicit}], "next_cursor": <PaginationToken or null>}`. `created` is the
    `UserCreateDate` ISO string. `email`/`status` from the user record.
  - `cursor` is the opaque Cognito `PaginationToken` passed straight through
    (no base64 wrapping needed — it is already an opaque string). Absent on
    the last page.
- `POST /api/admin/users/{username}/role` (admin-only), body `{"role": "admin"|"viewer"}`:
  - Validate `role ∈ {admin, viewer}` → else `400`.
  - **Self-demotion guard:** if `{username}` equals the caller's
    `cognito:username` (fallback `sub`) AND `role == "viewer"` → `409`
    `{"error": "cannot remove your own admin role"}`. This guarantees the
    acting admin stays admin, so the pool can never reach zero admins via this
    API (no global last-admin scan needed).
  - Apply exclusively:
    - `role == "admin"` → `admin_add_user_to_group(dbops-admin)` then
      `admin_remove_user_from_group(dbops-viewer)`.
    - `role == "viewer"` → `admin_add_user_to_group(dbops-viewer)` then
      `admin_remove_user_from_group(dbops-admin)`.
    - `admin_remove_user_from_group` is idempotent (no error if not a member);
      `admin_add_user_to_group` is idempotent.
  - Returns `{"username", "role"}` on `200`.
  - `UserNotFoundException` → `404`; any other boto error → `500` with a
    GENERIC message (never `str(e)` — no leaking internal detail; mirrors the
    dashboard str(e) lesson).
- Any other method/path → `405` / `404` as appropriate.

`_caller_username(event)`: decode the bearer payload, return
`claims["cognito:username"] or claims["sub"]` (used by the guard).

### Component 2 — CDK wiring (`cdk/stacks/agent_stack.py`)

Mirror the `onboarding_lambda` block:

```python
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

IAM is scoped to the single user-pool ARN. No `dynamodb`/`secretsmanager`
grants — Cognito-only.

### Component 3 — Frontend `/admin/users` page

- New route `frontend/src/app/admin/users/page.tsx` (admin-only; gated like
  `/settings`). Add a nav entry hidden for viewers (mirror how `/settings` is
  hidden via `isAdmin()`), and ensure the ⌘K command palette entry is
  likewise admin-only.
- A table: **email**, status, role badge (`admin` / `viewer`, with an
  "implicit" hint when `implicit`), and a role `<select>` (admin/viewer).
  Changing the select pops a confirm, then `POST .../role` via `authedFetch`,
  then refetches the list.
- The acting admin's own row has its role control **disabled** with a tooltip
  ("you cannot change your own role") — the client knows its own username from
  the decoded token (`getUserGroups` already decodes; add a small
  `getUsername()` helper returning `cognito:username || sub`). The server
  enforces the 409 regardless; the UI disable is cosmetic.
- "Load more" using `next_cursor` (consistent with the activity export cursor
  pattern, but here the token is Cognito's opaque `PaginationToken`).
- Korean copy for descriptions/empty-state per the i18n scope (role names and
  status stay as-is; `dbops-admin`/`dbops-viewer` are jargon kept verbatim).

## Data Flow

Browser (`authedFetch`, Bearer ID token) → API GW (JWT authorizer) →
`admin_users` Lambda → `_is_admin` gate → Cognito `cognito-idp` admin APIs →
JSON back. No DB, no cache, no cross-account.

## Error Handling

- `_is_admin` / `_decode_jwt_payload` never raise (exception-safe), denied →
  `403`.
- All Cognito calls wrapped; `UserNotFoundException` → `404`, otherwise a
  generic `500` (no `str(e)`).
- Malformed JSON body on POST → `400`.
- Role not in `{admin, viewer}` → `400`.

## Testing

- **Unit (`tests/unit/api/test_admin_users.py`):** mock `boto3.client("cognito-idp")`.
  - `GET` lists users; role derivation for admin / viewer / no-group(implicit).
  - `GET` passes `PaginationToken` through and returns `next_cursor`.
  - `POST role=admin` → add admin + remove viewer (assert both calls).
  - `POST role=viewer` (other user) → add viewer + remove admin.
  - `POST role=viewer` on SELF (route username == caller `cognito:username`) → `409`, no Cognito write.
  - `POST` bad role → `400`; malformed body → `400`.
  - `UserNotFoundException` → `404`.
  - **Admin-gate contract** (the just-hardened canonical form): no-bearer → `403`;
    `Bearer <garbage>` → `403`; viewer (`dbops-viewer`) → `403`; no-group → not-403;
    `dbops-admin` → not-403.
- **CDK:** snapshot test stays green (new construct added — update/accept snapshot).
- **Frontend:** `npm run build` clean.

## Security

- Admin-gated by the canonical fail-closed `_is_admin`.
- IAM scoped to the one user-pool ARN; only the four group/list actions.
- Self-demotion guard ⇒ the pool can never be driven to zero admins via this API.
- No create/delete/password/MFA surface (deferred; the AWS console covers
  those rarer, higher-blast-radius operations).
- Server is authoritative; the UI self-row disable is cosmetic.
