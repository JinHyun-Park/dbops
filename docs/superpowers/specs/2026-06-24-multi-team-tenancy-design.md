# Multi-Team Tenancy — Program Design

**Date:** 2026-06-24
**Status:** approved-direction (user delegated: "보편적으로 적용 가능한 방향으로 진행" — universal, additive, default-open). Decomposed into a 4-spec program; this doc is the program architecture + the T-1 sub-spec in implementable detail. T-2..T-4 are scoped here and get their own design+plan when reached.

## Context

DBOps is a fleet DBOps platform. Today **every authenticated user sees every
registered cluster** across all read surfaces (dashboard, clusters, reports,
approvals, cost, search) and the chat agent operates platform-wide. The only
access distinction is **role** (admin vs viewer, via Cognito groups
`dbops-admin`/`dbops-viewer`) which gates WRITES, not cluster visibility.

Operators running many clusters across teams want **team-scoped visibility**: a
DBA on team A should not see team B's clusters. This must be **additive and
backward-compatible** — an existing single-team deployment that never creates a
team keeps working exactly as today.

### Grounded current contract (verified, file:line)

- **Identity:** `api/*/handler.py::_decode_jwt_payload` + `_claims` read the
  Cognito JWT (client-decoded, no sig verify yet) → `cognito:username` (== `sub`)
  is the stable per-user key; `cognito:groups` carries the role. The membership
  key for teams is **`cognito:username`** (`api/admin_users/handler.py:54-56`
  `_caller_username`).
- **Role:** `_is_admin(event)` (duplicated in ~11 handlers, NO shared module —
  api/ Lambdas are independent packages, see memory) — admin if `dbops-admin` in
  groups OR **no groups at all** (single-admin-deploy fallback); viewer if
  `dbops-viewer`; **fail-closed** on missing/invalid bearer. Roles assigned via
  Cognito `admin_add_user_to_group` (`api/admin_users/handler.py`).
- **Cluster registry:** `foundation_stack.py:89` `clusters_table` — DynamoDB,
  PK=`cluster_id` (STRING), no sort key, **no team/owner attribute** (greenfield).
  Listed via `table.scan()` (`api/clusters/handler.py:31`), env `CLUSTERS_TABLE`.
- **Cluster-read surfaces (the leak-prevention inventory):** api/clusters
  (list), api/dashboard (~14 per-cluster routes), api/reports, api/approvals,
  api/cost (`?per_cluster`), api/saved_queries, search/logs, api/simulation,
  api/explain, api/alerts, api/scheduled_tasks, api/tasks, api/memory,
  api/context_files — each takes a `cluster_id` or lists clusters.
- **Agent/MCP gap:** `agent/server.py` has **no caller-identity extraction**;
  the chat agent + the 5 MCP servers are fully cluster-agnostic (cluster_id
  arrives as a tool param with no ownership check). This is the hardest surface
  → isolated into T-4.

## Architecture — the visibility overlay

A single conceptual primitive, applied everywhere a cluster is exposed:

**`visible_cluster_ids(event) -> Optional[set[str]]`**

- Returns `None` ⇒ "all clusters" (the caller is an **admin**, or tenancy is
  effectively off). Callers treat `None` as no-filter.
- Returns a `set` ⇒ the exact cluster*ids the caller may see: the union of
  (a) **unassigned clusters** (no `team_id`) — \_default-open*, and (b) clusters
  whose `team_id` is a team the caller is a **member** of.
- A **viewer with no team memberships** sees only unassigned clusters (today's
  behavior is preserved while no teams exist).

**Invariants (the security contract):**

1. **Admins always see all clusters** (management role) — overlay returns `None`.
2. **Unassigned cluster ⇒ visible to everyone** (additive: zero teams = today).
3. **Assigned cluster ⇒ visible only to its team's members + admins.**
4. **Default-open, never default-deny:** a cluster is hidden ONLY when it has an
   explicit `team_id` the caller isn't in. Missing data (no team*id, lookup
   error) fails **open to the current behavior** for reads (never hides a
   cluster a user sees today) — EXCEPT the overlay itself must be correct, so a
   membership-lookup failure for an \_assigned* cluster fails **closed** (hide),
   to avoid leaking an assigned cluster on a transient error. (Reads only;
   writes already gated by role.)

### Storage (DynamoDB — matches the existing pattern)

- **`teams`** table: PK=`team_id` (STRING). Attrs: `name`, `created_at`,
  `created_by`. (One row per team.)
- **`team_members`** table: PK=`team_id`, SK=`username` (Cognito username). One
  row per (team, member). Query-by-team for membership; a GSI on `username`
  (PK=`username`) gives "my teams" in O(1) for the per-request overlay.
- **cluster→team:** an additive **`team_id`** attribute on the existing
  `clusters_table` item (nullable; absent ⇒ unassigned ⇒ default-open). One team
  per cluster (YAGNI — many-to-many deferred; extensible later via a mapping
  table without breaking the overlay's set contract).

### The overlay helper — duplication vs layer

`_is_admin` is copied per-handler today. The overlay
(`visible_cluster_ids` + `cluster_visible(event, cluster_id)`) is more logic
(two DynamoDB reads) and will be needed by ~14 handlers. **Decision:** ship it as
a tiny self-contained module **vendored (copied) into each handler package that
needs it**, exactly like `_is_admin` / `engine_family.py` (4-copy pattern) — api/
Lambdas can't share imports, and a Lambda layer for api/ doesn't exist today.
A byte-identical-copy test (mirror the engine_family parity test) keeps the
copies in sync. (If the copy count becomes painful, a follow-up introduces an
api/ shared layer — out of scope here.)

## Decomposition (4 sub-specs, built in order)

- **T-1 Foundation + primary enforcement** (this doc, detailed below): teams +
  team_members tables + GSI; `team_id` on clusters; the `tenancy.py` overlay
  module (vendored) + parity test; admin teams CRUD API
  (`/api/admin/teams*`); cluster→team assignment; **enforce on the two primary
  read paths: `/api/clusters` (filter the scan) + `/api/dashboard/*` (403 if the
  target cluster isn't visible).** Backward-compatible default-open.
- **T-2 Full REST coverage:** vendor the overlay into + enforce on every
  remaining cluster-exposing handler (reports, approvals, cost per-cluster,
  saved_queries, search/logs, simulation, explain, alerts, scheduled_tasks,
  tasks, memory, context_files). The leak-prevention sweep. Per-handler list +
  the expected filter/403 behavior, with a test per handler.
- **T-3 Frontend:** admin **Teams** management UI in `/settings` (create team,
  manage members, assign clusters) — admin-gated, nav hidden for viewers
  (mirror admin_users). The cluster dropdown / shared selection store already
  consumes the now-filtered `/api/clusters`, so non-admins simply receive fewer
  clusters; verify the ⌘K palette + shared store handle an empty/!visible
  selection gracefully.
- **T-4 Agent/chat tenancy (highest risk, isolated):** forward the caller's
  identity into the AgentCore SSE entry → resolve `visible_cluster_ids` →
  constrain the agent (the system prompt receives only the visible cluster set;
  the agent must refuse to operate on a non-visible cluster) + a defense-in-depth
  ownership check before MCP tool execution. Designed separately when reached
  (touches agent/server.py, the SSE contract, and the gateway).

Each sub-spec produces working, independently-testable software and is
merged/deployed before the next.

---

## T-1 — Foundation + Primary Enforcement (implementable detail)

### Components

1. **CDK (`foundation_stack.py` + `agent_stack.py`):**

   - `teams_table` (PK `team_id`), `team_members_table` (PK `team_id`, SK
     `username`, **GSI `by-user`** PK `username`). Same removal_policy / billing
     as the sibling tables. Follow the `CLUSTERS_TABLE` name-prefix convention.
   - Grant the relevant Lambdas access: the new `admin_teams` Lambda
     (read/write teams + members + clusters_table team_id), and **read** on
     teams/team_members/clusters for the enforcement handlers (clusters,
     dashboard) — env vars `TEAMS_TABLE`, `TEAM_MEMBERS_TABLE`,
     `TEAM_MEMBERS_BY_USER_INDEX`.
   - API routes: `GET/POST /api/admin/teams`, `GET/DELETE
/api/admin/teams/{team_id}`, `POST/DELETE
/api/admin/teams/{team_id}/members/{username}`, `POST
/api/admin/teams/{team_id}/clusters/{cluster_id}` (assign) + `DELETE`
     (unassign). Regenerate `openapi.json` (route-table parity).

2. **`api/_tenancy/tenancy.py` (the vendored overlay module — source of truth),
   copied byte-identical into `api/clusters/` and `api/dashboard/` (and, in T-2,
   the rest):**

   - `my_team_ids(username) -> set[str]` — query `team_members` GSI by username.
   - `assigned_team_id(cluster_item) -> Optional[str]` — read `team_id` attr.
   - `visible_cluster_ids(event, all_cluster_items) -> Optional[set[str]]` —
     `None` if `_is_admin(event)`; else compute the allowed set per the
     invariants. (Takes the already-scanned items for the list path; a
     `cluster_visible(event, cluster_id, cluster_item)` variant for single-cluster
     paths does one membership lookup.)
   - Fail-open-to-current-behavior on infra errors for unassigned; fail-closed
     (hide) for an assigned cluster whose membership check errors.

3. **Enforcement:**

   - `api/clusters/handler.py` list: after the scan, if
     `visible_cluster_ids(event, items)` is not `None`, filter `items` to the
     allowed set. Admin → unchanged.
   - `api/dashboard/handler.py`: for any per-cluster route, resolve the cluster
     item, and if not visible to the caller → **403** (admin-required-style
     body, Korean note). Admin → unchanged.

4. **`api/admin_teams/handler.py`** (admin-gated, mirror `admin_users`):
   - All routes `_forbid_viewer`-gated (fail-closed `_is_admin`).
   - Teams CRUD; member add/remove; cluster assign/unassign (writes
     `clusters_table` item `team_id`, or removes it). DynamoDB **scan/query must
     paginate** (memory gotcha). Deleting a team unassigns its clusters
     (clear `team_id`) — no dangling assignment.

### Data flow

Admin creates a team + adds members (Cognito usernames) + assigns clusters →
`team_id` written on the cluster item + membership rows in `team_members`. On
every `/api/clusters` list and `/api/dashboard/*` read, the overlay resolves the
caller's visible set and filters/403s. Unassigned clusters stay visible to all.

### Error handling

- Overlay infra error (DynamoDB): unassigned clusters stay visible (fail-open to
  today); an assigned cluster with a failed membership check is hidden
  (fail-closed) — never leak an assigned cluster on error.
- Admin teams API: validate team_id/username/cluster_id exist; 404 on missing;
  paginate all scans; idempotent assign/unassign.
- `_is_admin` stays fail-closed (no bearer → not admin → overlay returns a
  restricted set, not `None`).

### Testing

- **Overlay unit** (`tests/unit/api/test_tenancy.py`): admin → `None` (all);
  viewer with no teams → only unassigned; viewer in team A → unassigned + team-A
  clusters, NOT team-B; assigned-cluster membership-error → hidden (fail-closed);
  unassigned with infra error → visible (fail-open).
- **clusters list** : admin sees all; viewer sees filtered; zero-teams
  deployment → viewer sees all (backward compat).
- **dashboard**: viewer hitting a non-visible cluster → 403; visible → 200;
  admin → always 200.
- **admin_teams**: CRUD + member + assign/unassign happy paths; viewer → 403 on
  every route (raw-token live-smoke style — the priv-esc gotcha); pagination;
  delete-team clears cluster team_id.
- **vendored-copy parity** (mirror engine_family test): the `tenancy.py` copies
  are byte-identical.
- Full unit suite + CDK synth green; openapi route parity.

### Security

- Default-open additive: no team ⇒ no behavior change (backward compatible).
- Admin-gated management (fail-closed `_is_admin`, viewer 403 on all
  `/api/admin/teams*`).
- Reads scoped by membership; writes already role-gated. The overlay never
  _grants_ access beyond today — it only _removes_ assigned clusters from
  non-members.
- T-1 covers the two primary read paths; **T-2 closes the rest (the full
  inventory) — until T-2 ships, the other read endpoints remain platform-wide,
  so T-1 is NOT a complete isolation boundary on its own.** This is stated so the
  partial coverage isn't mistaken for full tenancy. T-4 covers the agent.

## Out of scope (this program)

- Cross-account team federation; per-team RBAC beyond admin/viewer; many-to-many
  cluster↔team; Cedar enforcement (LOG_ONLY by decision). JWT signature
  verification (separate pre-existing follow-up).
