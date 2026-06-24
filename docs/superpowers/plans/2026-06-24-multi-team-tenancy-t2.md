# Multi-Team Tenancy T-2 (Full REST Coverage) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the T-1 cluster-visibility overlay to every remaining non-admin-reachable REST endpoint that returns cluster-scoped data, closing the leak-prevention sweep (reports, approvals, cost, saved_queries, alerts, scheduled_tasks, tasks, simulation).

**Architecture:** Add one convenience function `visible_set_from_registry(event)` to the existing vendored `tenancy.py` (scans `CLUSTERS_TABLE` for `{cluster_id, team_id}` and returns the visible set, `None` for admins), then vendor `tenancy.py` into each of the 8 handler packages and apply the established pattern: LIST endpoints filter rows to the visible set; single-resource reads 403 when the row's `cluster_id` isn't visible; cost filters its per-cluster rows; ungated writes (tasks/scheduled_tasks/simulation) 403 on the body `cluster_id`. Admin → no-op (sees all). Default-open + backward-compatible: unassigned clusters stay visible; zero teams = today.

**Tech Stack:** Python 3.12 (api/ Lambdas, boto3 DynamoDB), AWS CDK (Python).

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in commits.
- **READ-focused, non-admin-reachable only:** apply the gate where a NON-ADMIN can reach cluster-scoped data. Admin-only write paths (approvals PUT/POST, saved_queries CRUD, alerts POST/DELETE) are SKIPPED — admins see all, so the gate is a no-op there; do not add it (avoids dead code). Ungated writes (tasks POST, scheduled_tasks POST/DELETE, simulation POST) ARE gated on the body/row `cluster_id` because a viewer can reach them.
- **explain is SKIPPED** — it is admin-only (`api/explain/handler.py:145` `_is_admin`); admins see all clusters, so a tenancy gate is a no-op. Document the skip; do not add a redundant gate.
- **Admin → no-op:** `visible_cluster_ids`/`visible_set_from_registry` return `None` and `cluster_visible` returns `True` for admins. Never filter an admin.
- **Default-open / backward-compatible:** unassigned cluster (no `team_id`) visible to all; zero teams ⇒ identical to today.
- **Fail behavior:** `my_team_ids` empty-on-error (assigned hidden / unassigned visible); `visible_set_from_registry` returns `None` (no filter, fail-open to current behavior) on a registry-scan failure — consistent with the T-1 fleet filter (don't blank a list on a transient DDB outage). `cluster_visible` single-resource gate stays fail-closed for an assigned cluster on membership error.
- **`tenancy.py` is VENDORED byte-identical** across ALL copies — extend the parity test (`tests/unit/api/test_tenancy_parity.py`) to list every copy. The canonical content lives in `api/clusters/tenancy.py`; every other copy must be `cp`-identical.
- **All DynamoDB scans/queries PAGINATE** (1MB truncation gotcha).
- **No new API routes** (enforcement is added inside existing handlers) ⇒ openapi.json unchanged. If any task somehow adds a route, regen `frontend/public/openapi.json`.
- **CDK:** each enforcing Lambda needs `TEAM_MEMBERS_TABLE` + `TEAM_MEMBERS_BY_USER_INDEX="by-user"` + `CLUSTERS_TABLE` env (most already have CLUSTERS_TABLE) and `foundation.team_members_table.grant_read_data(<lambda>)` + (if absent) `clusters_table.grant_read_data`. Mirror the T-1 clusters/dashboard wiring.
- Korean copy for 403 notes ("이 클러스터에 대한 접근 권한이 없습니다."); identifiers verbatim.

**Grounding:** the full per-handler inventory (cluster_id source + enforcement per route, with file:line) is at `.superpowers/sdd/t2-handler-inventory.md` — implementers MUST read the relevant handler's section there.

---

### Task 1: Add `visible_set_from_registry` to the overlay + re-vendor

**Files:**

- Modify: `api/clusters/tenancy.py` (canonical — add the function), then re-copy to `api/dashboard/tenancy.py`
- Test: `tests/unit/api/test_tenancy.py` (add cases), `tests/unit/api/test_tenancy_parity.py` (unchanged — still 2 copies until Task 2+)

**Interfaces:**

- Produces: `visible_set_from_registry(event) -> set[str] | None` — `None` for admin or on registry-scan failure (fail-open); else the visible cluster_id set computed from a `CLUSTERS_TABLE` scan of `{cluster_id, team_id}`.

- [ ] **Step 1: Write the failing test** in `tests/unit/api/test_tenancy.py`:

```python
def test_visible_set_from_registry_admin_returns_none(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    assert t.visible_set_from_registry(_event(groups=["dbops-admin"])) is None


def test_visible_set_from_registry_filters_viewer(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    fake = MagicMock()
    fake.scan.return_value = {"Items": [
        {"cluster_id": "c-open"},
        {"cluster_id": "c-teamA", "team_id": "tA"},
        {"cluster_id": "c-teamB", "team_id": "tB"},
    ]}
    with patch.object(t.boto3, "resource", return_value=MagicMock(**{"Table.return_value": fake})), \
         patch.object(t, "my_team_ids", return_value={"tA"}):
        s = t.visible_set_from_registry(_event(groups=["dbops-viewer"]))
    assert s == {"c-open", "c-teamA"}


def test_visible_set_from_registry_scan_error_fails_open_none(monkeypatch):
    t = _load()
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    with patch.object(t.boto3, "resource", side_effect=RuntimeError("ddb down")):
        assert t.visible_set_from_registry(_event(groups=["dbops-viewer"])) is None
```

- [ ] **Step 2: Run it to verify it fails** — `python -m pytest tests/unit/api/test_tenancy.py -k visible_set -q` → FAIL.

- [ ] **Step 3: Add the function** to `api/clusters/tenancy.py` (after `visible_cluster_ids`):

```python
def visible_set_from_registry(event):
    """Convenience for LIST handlers that don't already hold the cluster
    registry: scan CLUSTERS_TABLE for {cluster_id, team_id} and return the
    visible cluster_id set. None for admins (no filter). On a registry-scan
    failure returns None (fail-open to current behavior — consistent with the
    fleet filter; a transient DDB outage must not blank a list)."""
    if is_admin(event):
        return None
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        return None
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.scan(ProjectionExpression="cluster_id, team_id")
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.scan(
                ProjectionExpression="cluster_id, team_id",
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
    except Exception as e:
        print(f"[tenancy] visible_set_from_registry scan failed: {e}")
        return None
    return visible_cluster_ids(event, items)
```

- [ ] **Step 4: Re-copy to `api/dashboard/tenancy.py`** — `cp api/clusters/tenancy.py api/dashboard/tenancy.py` (keep byte-identical).

- [ ] **Step 5: Run tests** — `python -m pytest tests/unit/api/test_tenancy.py tests/unit/api/test_tenancy_parity.py -q` → PASS.

- [ ] **Step 6: Commit.**

```bash
git add api/clusters/tenancy.py api/dashboard/tenancy.py tests/unit/api/test_tenancy.py
git commit -m "feat(tenancy): visible_set_from_registry helper for LIST filtering"
```

---

### Task 2: Enforce on reports + saved_queries

**Files:**

- Create: `api/reports/tenancy.py`, `api/saved_queries/tenancy.py` (cp from `api/clusters/tenancy.py`)
- Modify: `api/reports/handler.py`, `api/saved_queries/handler.py`, `cdk/stacks/agent_stack.py`, `tests/unit/api/test_tenancy_parity.py`
- Test: `tests/unit/api/test_reports_tenancy.py`, `tests/unit/api/test_saved_queries_tenancy.py`

**Interfaces:** Consumes `tenancy.visible_set_from_registry`, `tenancy.cluster_visible` (Task 1).

Read `.superpowers/sdd/t2-handler-inventory.md` §1 (reports) + §4 (saved_queries) for exact line numbers.

- [ ] **Step 1: cp the overlay** — `cp api/clusters/tenancy.py api/reports/tenancy.py` and `cp api/clusters/tenancy.py api/saved_queries/tenancy.py`. Add both to the parity list in `tests/unit/api/test_tenancy_parity.py` (`_COPIES`).

- [ ] **Step 2: Write failing tests** — `tests/unit/api/test_reports_tenancy.py` and `test_saved_queries_tenancy.py`. For each (load handler via importlib like the existing per-handler tests):

  - **reports LIST** (`GET /api/reports`, no `?cluster_id`): mock the DB query to return rows for `c-open`/`c-teamA`/`c-teamB`; patch `tenancy.visible_set_from_registry`→`{"c-open","c-teamA"}`; assert the response excludes `c-teamB`. Admin (`visible_set_from_registry`→None) → all rows.
  - **reports GET /{id}**: mock the row fetch to return `{cluster_id: "c-teamB", ...}`; patch `tenancy.cluster_visible`→False; assert 403. Visible → 200.
  - **reports GET /{id}/html**: same 403-by-row-cluster_id (fetch the row's `cluster_id`/`s3_key`, gate before presign).
  - **saved_queries LIST + GET /{id}** mirror reports.

- [ ] **Step 3: Run → fail.**

- [ ] **Step 4: Implement reports** (`api/reports/handler.py`): `import tenancy`.

  - LIST arm (after building `rows`): `visible = tenancy.visible_set_from_registry(event); if visible is not None: rows = [r for r in rows if r.get("cluster_id") in visible]`.
  - `{id}` + `{id}/html` arms: after fetching the row (which carries `cluster_id`), `if not tenancy.cluster_visible(event, {"team_id": _team_id_for(row_cluster_id)}): return 403`. Since the row gives `cluster_id` but not `team_id`, resolve via the registry: add a small `_cluster_item(cluster_id)` that `get_item`s `CLUSTERS_TABLE` (mirror dashboard's `_lookup_cluster`), and pass it to `cluster_visible`. Korean 403 note.

- [ ] **Step 5: Implement saved_queries** (`api/saved_queries/handler.py`): `import tenancy`; LIST filter + GET /{id} 403 via the same `_cluster_item` + `cluster_visible` pattern. (Writes are admin-gated at `:270`/`:302` → SKIP per Global Constraints.)

- [ ] **Step 6: CDK** — on the `reports_lambda` and `saved_queries_lambda` blocks in `agent_stack.py`, add `TEAM_MEMBERS_TABLE` + `TEAM_MEMBERS_BY_USER_INDEX="by-user"` env (+ `CLUSTERS_TABLE` if absent) and `foundation.team_members_table.grant_read_data(<lambda>)` + `foundation.clusters_table.grant_read_data(<lambda>)` if not already granted.

- [ ] **Step 7: Run** — `python -m pytest tests/unit/api/test_reports_tenancy.py tests/unit/api/test_saved_queries_tenancy.py tests/unit/api/test_tenancy_parity.py -q` → PASS; `python -m pytest tests/unit/api -q` → no regression; `python -m pytest tests/cdk/test_synth.py -q` → PASS.

- [ ] **Step 8: Commit.** `feat(tenancy): enforce cluster visibility on reports + saved_queries reads`

---

### Task 3: Enforce on approvals + alerts

**Files:** Create `api/approvals/tenancy.py`, `api/alerts/tenancy.py` (cp); modify both handlers + `agent_stack.py` + parity test; tests `test_approvals_tenancy.py`, `test_alerts_tenancy.py`.

Read inventory §2 (approvals) + §5 (alerts).

- [ ] **Step 1: cp the overlay** into both packages; add to the parity list.

- [ ] **Step 2: Write failing tests:**

  - **approvals**: `GET /api/approvals` list + `/api/approvals/activity` → filter rows to visible (rows carry `cluster_id`); `GET /api/approvals/{id}` → 403 when the fetched item's `cluster_id` not visible. (POST/PUT admin-gated at `:321`/`:398` → SKIP.)
  - **alerts**: `GET /api/alerts` list → filter; `GET /api/alerts/{id}/impact` → 403 by the rule's `cluster_id`. (POST/DELETE `_forbid_viewer`-gated at `:593`/`:598` → SKIP.)

- [ ] **Step 3: Run → fail.**

- [ ] **Step 4: Implement approvals** — `import tenancy`; in the list + activity arms, `visible = tenancy.visible_set_from_registry(event)` and filter rows by `cluster_id` when not None; in the `{id}` arm, gate via `cluster_visible` against the registry item for the fetched `cluster_id`.

- [ ] **Step 5: Implement alerts** — `import tenancy`; list filter; `/impact` 403 by the rule's `cluster_id` (resolve the registry item, `cluster_visible`).

- [ ] **Step 6: CDK** — add team_members env + grant (+ CLUSTERS_TABLE/grant if absent) to `approvals_lambda` and `alerts_lambda`.

- [ ] **Step 7: Run** the two new test files + `tests/unit/api -q` + synth → PASS.

- [ ] **Step 8: Commit.** `feat(tenancy): enforce cluster visibility on approvals + alerts reads`

---

### Task 4: Enforce on tasks + scheduled_tasks

**Files:** Create `api/tasks/tenancy.py`, `api/scheduled_tasks/tenancy.py` (cp); modify both handlers + `agent_stack.py` + parity test; tests `test_tasks_tenancy.py`, `test_scheduled_tasks_tenancy.py`.

Read inventory §6 (scheduled_tasks) + §7 (tasks). NOTE: these handlers have UNGATED writes (no `_is_admin`/`_forbid_viewer`), so a viewer can reach POST/DELETE — gate the body/row `cluster_id` too.

- [ ] **Step 1: cp the overlay** into both packages; add to the parity list.

- [ ] **Step 2: Write failing tests:**

  - **tasks**: `GET /api/tasks` list (no `?cluster`) → filter to visible; `GET /api/tasks/{id}` → 403 by item `cluster_id`; `POST /api/tasks` with a non-visible `body.cluster_id` → 403.
  - **scheduled_tasks**: `GET` list → filter; `POST` with non-visible `body.cluster_id` → 403; `DELETE /{id}` → 403 by the task's `cluster_id`.

- [ ] **Step 3: Run → fail.**

- [ ] **Step 4: Implement tasks** — `import tenancy`; list filter (`visible_set_from_registry`); `{id}` GET 403 (`cluster_visible` on the registry item for the item's `cluster_id`); POST: after parsing `body.cluster_id`, 403 if `not cluster_visible(event, _cluster_item(body_cluster_id))`.

- [ ] **Step 5: Implement scheduled_tasks** — same three gates; the existing `_cluster_exists` registry lookup can be reused/extended to fetch `team_id` for the gate.

- [ ] **Step 6: CDK** — team_members env + grant (+ CLUSTERS_TABLE/grant if absent) on `tasks_lambda` + `scheduled_tasks_lambda`.

- [ ] **Step 7: Run** the two test files + `tests/unit/api -q` + synth → PASS.

- [ ] **Step 8: Commit.** `feat(tenancy): enforce cluster visibility on tasks + scheduled_tasks (incl. ungated writes)`

---

### Task 5: Enforce on cost + simulation

**Files:** Create `api/cost/tenancy.py`, `api/simulation/tenancy.py` (cp); modify both handlers + `agent_stack.py` + parity test; tests `test_cost_tenancy.py`, `test_simulation_tenancy.py`.

Read inventory §3 (cost) + §9 (simulation).

- [ ] **Step 1: cp the overlay** into both packages; add to the parity list (this completes the copy set: clusters, dashboard, reports, saved_queries, approvals, alerts, tasks, scheduled_tasks, cost, simulation = 10 copies).

- [ ] **Step 2: Write failing tests:**

  - **cost**: `?view=rds` and `?view=elasticache` — when `per_cluster` rows are present, mock `_query_per_cluster` to return rows for `c-open`/`c-teamA`/`c-teamB`; patch `tenancy.visible_set_from_registry`→`{"c-open","c-teamA"}`; assert the response `per_cluster` excludes `c-teamB`. Admin → unfiltered. (The per-cluster row's cluster identifier is the cost-allocation tag value — match it against the visible set by `cluster`/`cluster_id` key as the rows expose it.)
  - **simulation**: any tool POST with a non-visible `body.cluster_id` → 403 before dispatch; visible/admin → proceeds.

- [ ] **Step 3: Run → fail.**

- [ ] **Step 4: Implement cost** — `import tenancy`; in `_handle_rds_view` + `_handle_elasticache_view`, after `per_cluster` is built, `visible = tenancy.visible_set_from_registry(event)` and if not None filter `per_cluster` to rows whose cluster key is in `visible`. (The view handlers must receive `event` — thread it through from `lambda_handler`.) Leave the totals/by-usage-type unfiltered (account-level aggregates, not per-cluster — note this is an intentional limitation: only the per-cluster breakdown is tenant-scoped).

- [ ] **Step 5: Implement simulation** — `import tenancy`; in `lambda_handler` after `body.cluster_id` is parsed (`:1130`), `if not tenancy.cluster_visible(event, _cluster_item(cluster_id)): return 403` before dispatching any tool. Add a `_cluster_item` registry get_item helper if absent.

- [ ] **Step 6: CDK** — team_members env + grant (+ CLUSTERS_TABLE/grant if absent) on `cost_lambda` + `simulation_lambda`. (cost reads no DB today — it needs CLUSTERS_TABLE env + grant for `visible_set_from_registry`.)

- [ ] **Step 7: Run** the two test files + `tests/unit -q` (full, no regression) + synth → PASS.

- [ ] **Step 8: Commit.** `feat(tenancy): enforce cluster visibility on cost per-cluster + simulation`

---

## Post-implementation (controller, after all tasks reviewed clean)

- **Final whole-branch review (opus — isolation sweep):** verify every non-admin-reachable cluster-scoped read across the 8 handlers is gated (LIST filter / single 403 / cost per-cluster filter / ungated-write 403); admins are never filtered; default-open + zero-teams backward-compat holds; the SKIP decisions (explain admin-only; admin-gated writes) are correct (no viewer-reachable leak left); `visible_set_from_registry` projection includes `team_id` (the T-1 projection-trap lesson — confirm against real source); all 10 `tenancy.py` copies byte-identical (parity test lists all); scans paginate. **Confirm the coverage claim: after T-2, all REST cluster reads are tenant-scoped; the AGENT (chat/MCP) remains platform-wide until T-4 — do not over-claim.**
- **Deploy dev:** `cdk deploy dbops-dev-agent` (the 8 Lambdas gain team_members env+grant). No foundation change.
- **Live smoke (viewer token + seeded test team, mirror T-1):** seed a team with the e2e viewer as member + one assigned cluster + one other-team cluster. Verify for EACH newly-gated surface: `GET /api/reports` (+ a report by id on the other-team cluster → 403), `GET /api/saved-queries`, `GET /api/approvals/activity`, `GET /api/alerts`, `GET /api/tasks`, `GET /api/scheduled-tasks`, `GET /api/cost?view=rds` (per_cluster excludes other-team) — each excludes the other-team cluster; the unassigned cluster stays visible. Clean up the test team.
- Then `superpowers:finishing-a-development-branch` (ff-merge to main). T-3 (frontend) follows.
