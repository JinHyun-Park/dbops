# Multi-Team Tenancy T-3 (Frontend Teams Admin UI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin-only `/admin/teams` page to create teams, manage members, and assign clusters — consuming the `/api/admin/teams*` API shipped in T-1, mirroring the existing `/admin/users` admin console for visual + structural consistency.

**Architecture:** Frontend-only (Next.js 16 static export). Task 1 adds the api-client functions + types for the teams API. Task 2 adds the `/admin/teams` page (team list + create + a detail view managing members + cluster assignments) and a nav entry, mirroring `src/app/admin/users/page.tsx` and the `adminOnly` nav pattern in `app-shell.tsx`.

**Tech Stack:** Next.js 16 (App Router, static export), TypeScript, Tailwind, the project's `@/components/design-system/page-shell` primitives.

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in commits.
- **Product-quality UI, not AI-generated feel** (user rule) — mirror `src/app/admin/users/page.tsx` EXACTLY for layout, zinc palette, typography, spacing, Korean copy, empty/loading/error states. Do not invent a new visual language.
- **Admin-gated, cosmetic + server-enforced:** the nav item is `adminOnly: true` (hidden for viewers via `isAdmin()`); the page shows the admin-only `EmptyState` on a 403 (mirror admin/users `adminOnly` handling). The server (`/api/admin/teams*`) is the real gate (T-1) — the UI gate is cosmetic.
- **Korean copy** for all labels/descriptions/confirms; identifiers (team_id, cluster_id, username) verbatim/mono.
- **Reuse, don't duplicate:** member-picker uses `fetchAdminUsers`; cluster-picker uses `fetchClusters` (each cluster item already carries `cluster_id` + `team_id`); design-system `PageBody/PageHeader/Section/EmptyState`.
- **Destructive actions confirm:** delete-team + remove-member + unassign-cluster use `window.confirm` with a Korean message (mirror admin/users `onChangeRole`).
- **No `aws s3 sync` / deploy in the tasks** — controller deploys after review.
- Verification is `npm run build` (typecheck + static prerender); there are no frontend unit tests in this repo.

**Grounding (read before implementing):**

- Mirror page: `src/app/admin/users/page.tsx` (the full template — copy its structure).
- api-client patterns: `src/lib/api-client.ts` `fetchAdminUsers`/`updateUserRole` (~:2540) + `AdminUser` interface (~:2520) + `fetchClusters` (returns the cluster array; items have `cluster_id`, `team_id?`).
- Nav: `src/components/app-shell.tsx` — the admin group (~:200-237) with `adminOnly: true` items (`/admin/users` at :220, icon from `lucide-react`, `hint`); `isAdmin()` filters them (set at :512).
- Auth: `src/lib/auth.ts` `isAdmin()`, `getUsername()`.
- The T-1 API response shapes: `GET /api/admin/teams` → `{teams: [{team_id, name, created_at, created_by, member_count}]}`; `GET /api/admin/teams/{team_id}` → `{team_id, name, members: string[], clusters: string[]}`; `POST /api/admin/teams {name}` → `{team_id, name}`; `POST/DELETE /api/admin/teams/{team_id}/members/{username}`; `POST/DELETE /api/admin/teams/{team_id}/clusters/{cluster_id}`; `DELETE /api/admin/teams/{team_id}` → `{team_id, deleted: true}`.

---

### Task 1: api-client functions + types for the teams API

**Files:**

- Modify: `frontend/src/lib/api-client.ts` (add the `AdminTeam` + `TeamDetail` types + 8 functions near the existing `fetchAdminUsers`)

**Interfaces:**

- Produces:

  - `interface AdminTeam { team_id: string; name: string; created_at?: string; created_by?: string; member_count: number }`
  - `interface TeamDetail { team_id: string; name: string; members: string[]; clusters: string[] }`
  - `fetchAdminTeams(): Promise<{ teams: AdminTeam[] }>`
  - `fetchTeamDetail(teamId: string): Promise<TeamDetail>`
  - `createTeam(name: string): Promise<{ team_id: string; name: string }>`
  - `deleteTeam(teamId: string): Promise<{ team_id: string; deleted: boolean }>`
  - `addTeamMember(teamId: string, username: string): Promise<void>`
  - `removeTeamMember(teamId: string, username: string): Promise<void>`
  - `assignClusterToTeam(teamId: string, clusterId: string): Promise<void>`
  - `unassignClusterFromTeam(teamId: string, clusterId: string): Promise<void>`

- [ ] **Step 1: Read** `src/lib/api-client.ts` `fetchAdminUsers`/`updateUserRole` (~:2540) to copy the exact `authedFetch` + `apiUrl` + `enc` + 403→`throw new Error("admin only")` + non-ok→Korean-error idiom.

- [ ] **Step 2: Add the types + functions** near the admin-users block. Each mirrors the admin-users error handling (403 → `throw new Error("admin only")`; non-ok → `throw new Error("...실패 (상태 ${res.status})")`). Example shape (apply to all):

```typescript
export interface AdminTeam {
  team_id: string;
  name: string;
  created_at?: string;
  created_by?: string;
  member_count: number;
}

export interface TeamDetail {
  team_id: string;
  name: string;
  members: string[];
  clusters: string[];
}

export async function fetchAdminTeams(): Promise<{ teams: AdminTeam[] }> {
  const res = await authedFetch(await apiUrl("/api/admin/teams"));
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`팀 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchTeamDetail(teamId: string): Promise<TeamDetail> {
  const res = await authedFetch(
    await apiUrl(`/api/admin/teams/${enc(teamId)}`),
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`팀 상세 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function createTeam(
  name: string,
): Promise<{ team_id: string; name: string }> {
  const res = await authedFetch(await apiUrl("/api/admin/teams"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`팀 생성 실패 (상태 ${res.status})`);
  return res.json();
}

export async function deleteTeam(
  teamId: string,
): Promise<{ team_id: string; deleted: boolean }> {
  const res = await authedFetch(
    await apiUrl(`/api/admin/teams/${enc(teamId)}`),
    { method: "DELETE" },
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`팀 삭제 실패 (상태 ${res.status})`);
  return res.json();
}

async function _teamMutate(
  path: string,
  method: "POST" | "DELETE",
  failMsg: string,
): Promise<void> {
  const res = await authedFetch(await apiUrl(path), { method });
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`${failMsg} (상태 ${res.status})`);
}

export async function addTeamMember(
  teamId: string,
  username: string,
): Promise<void> {
  return _teamMutate(
    `/api/admin/teams/${enc(teamId)}/members/${enc(username)}`,
    "POST",
    "멤버 추가 실패",
  );
}
export async function removeTeamMember(
  teamId: string,
  username: string,
): Promise<void> {
  return _teamMutate(
    `/api/admin/teams/${enc(teamId)}/members/${enc(username)}`,
    "DELETE",
    "멤버 제거 실패",
  );
}
export async function assignClusterToTeam(
  teamId: string,
  clusterId: string,
): Promise<void> {
  return _teamMutate(
    `/api/admin/teams/${enc(teamId)}/clusters/${enc(clusterId)}`,
    "POST",
    "클러스터 할당 실패",
  );
}
export async function unassignClusterFromTeam(
  teamId: string,
  clusterId: string,
): Promise<void> {
  return _teamMutate(
    `/api/admin/teams/${enc(teamId)}/clusters/${enc(clusterId)}`,
    "DELETE",
    "클러스터 할당 해제 실패",
  );
}
```

(Confirm `apiUrl` vs `api` — `fetchAdminUsers` uses `apiUrl`; match it. Confirm `enc` is the same encoder used by the admin-users functions.)

- [ ] **Step 3: Build** — `cd frontend && npm run build` → PASS (no type errors).

- [ ] **Step 4: Commit.** `git add frontend/src/lib/api-client.ts && git commit -m "feat(tenancy): api-client functions for the admin Teams API"`

---

### Task 2: `/admin/teams` page + nav entry

**Files:**

- Create: `frontend/src/app/admin/teams/page.tsx`
- Modify: `frontend/src/components/app-shell.tsx` (add the `/admin/teams` nav item)

**Interfaces:** Consumes Task 1's `fetchAdminTeams`/`fetchTeamDetail`/`createTeam`/`deleteTeam`/`add|removeTeamMember`/`assign|unassignClusterFromTeam` + existing `fetchAdminUsers`, `fetchClusters`, `isAdmin`/`getUsername`.

- [ ] **Step 1: Add the nav item** in `app-shell.tsx` — directly after the `/admin/users` item in the admin group, mirroring its shape:

```typescript
      {
        href: "/admin/teams",
        label: "Teams",
        icon: Users,
        adminOnly: true,
        hint: "팀 관리 — 멤버·클러스터 가시성 (관리자)",
      },
```

(Import `Users` from `lucide-react` if not already imported. Use an icon already in the file's import set if `Users` isn't available — pick the closest, e.g. `UsersRound`/`UserCheck`.)

- [ ] **Step 2: Create `src/app/admin/teams/page.tsx`** — mirror `src/app/admin/users/page.tsx`'s structure (`"use client"`, `PageBody/PageHeader/Section/EmptyState`, the `adminOnly` 403 empty state, loading/error states, zinc table styling, Korean copy). The page:

  - Loads `fetchAdminTeams()` on mount; 403 → admin-only `EmptyState` (eyebrow "접근 제한", title "관리자 전용 페이지").
  - **Team list** (Section eyebrow "Teams"): a table — name · member_count · (load-detail-on-click). A "팀 만들기" inline input + button calling `createTeam(name)` then reloading.
  - **Selected-team detail** (Section, shown when a team is clicked → `fetchTeamDetail`):
    - **Members:** the team's `members` (usernames; show the email if resolvable from a `fetchAdminUsers` map) each with a "제거" button (`removeTeamMember` + `window.confirm`); an "추가" control = a `<select>` of users NOT already members (from `fetchAdminUsers`) → `addTeamMember`.
    - **Clusters:** the team's `clusters` (cluster_ids) each with a "할당 해제" button (`unassignClusterFromTeam` + confirm); an "할당" control = a `<select>` of clusters not already on THIS team (from `fetchClusters`; a cluster already on another team can be reassigned — assigning overwrites its `team_id`, so show its current team if any) → `assignClusterToTeam`.
    - A "팀 삭제" button (`deleteTeam` + `window.confirm` warning that its clusters will be unassigned) — mirror the destructive-action confirm idiom.
  - After each mutation, re-fetch the detail (and the list for member/cluster counts) so the UI reflects server state; show a transient error banner on failure (mirror admin/users `error`).
  - Use optimistic-free re-fetch (simpler + correct) OR optimistic updates mirroring admin/users — either is fine; prefer re-fetch for the detail to stay consistent.

- [ ] **Step 3: Build** — `cd frontend && npm run build` → PASS, `/admin/teams` prerenders, no type errors.

- [ ] **Step 4: Commit.** `git add frontend/src/app/admin/teams/page.tsx frontend/src/components/app-shell.tsx && git commit -m "feat(tenancy): admin Teams management page (create, members, cluster assignment)"`

---

## Post-implementation (controller, after both tasks reviewed clean)

- **Final review (standard model — frontend, mirrors an existing page):** the page mirrors admin/users (consistent product UI, no AI-slop); all mutations confirm destructive actions; 403 → admin-only empty state; the nav item is `adminOnly`; member/cluster pickers reuse `fetchAdminUsers`/`fetchClusters`; no secrets/PII in URLs; Korean copy correct.
- **Build + deploy:** `cd frontend && npm run build` → `aws s3 sync out/ s3://dbops-dev-frontend-123456789012 --delete --exclude config.json --region ap-northeast-2` → CloudFront invalidate `E1234567890ABC`.
- **Live smoke:** as the e2e VIEWER (no admin token available), confirm the COSMETIC gate: `/admin/teams` shows the admin-only empty state and the nav "Teams" item is hidden for the viewer. (The admin CRUD happy-path is covered by the `test_admin_teams.py` unit suite + the api-level T-1 work; a full admin browser flow needs an admin credential, which isn't available in this environment — note this in the smoke report rather than faking it.)
- Then `superpowers:finishing-a-development-branch` (ff-merge to main). T-4 (agent SSE tenancy) is the remaining increment.
