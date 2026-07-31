# Admin-Gate Model Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify every `_is_admin` (11 API handlers) + the frontend `isAdmin()` to one hardened, fail-closed canonical form — closing two real fail-OPEN priv-escs (saved_queries/runbooks) + the garbage-token + unknown-group holes, while preserving the single-admin (no-group→admin) dev fallback.

**Architecture:** Apply the canonical `_is_admin` verbatim to all 11 handlers; apply the matching `isAdmin()` to the frontend. Behavior contract (the only thing that changes): no-bearer → deny; `Bearer <garbage>` → deny; no-group → admin; groups-without-`dbops-admin` → deny; `dbops-admin` → admin.

**Tech Stack:** Python 3.12 Lambdas, Next.js 16 (TypeScript).

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **Canonical form is identical across all handlers** (each keeps its own `_decode_jwt_payload`). Only the `_is_admin` body changes — do NOT touch the rest of any handler.
- **Preserve the single-admin fallback:** a VALID token with NO group claim (`cognito:groups` absent or `[]`) → admin. Do NOT require explicit `dbops-admin` membership (that would lock out fresh single-admin deploys).
- **Deny semantics (the tightening):** `if groups and "dbops-admin" not in groups: return False` — any non-empty group set lacking `dbops-admin` (viewer, or any other group) is denied.
- **Fail-closed:** no `Bearer ` prefix → False; unparseable token (empty claims) → False; non-list groups → False.
- **No behavior change for admin/viewer with Bearer:** existing tests using `Bearer`+`dbops-admin` (admin) / `dbops-viewer` (deny) must still pass.

The canonical Python `_is_admin` (reference — apply to every handler):

```python
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
    if groups and "dbops-admin" not in groups:
        return False
    return True
```

---

### Task 1: Unify all Python `_is_admin` to the canonical hardened form

**Files (each `_is_admin` body → canonical form):**

- Modify: `api/saved_queries/handler.py`, `api/runbooks/handler.py` (FAIL-OPEN → fixed)
- Modify: `api/clusters/handler.py`, `api/alerts/handler.py`, `api/approvals/handler.py`, `api/explain/handler.py`, `api/backups/handler.py` (add `if not claims` + tighten group check)
- Modify: `api/config/handler.py`, `api/approval_policies/handler.py`, `api/context_files/handler.py`, `api/onboarding/handler.py` (tighten the group-check line only)
- Test: extend the existing per-handler tests + add contract tests for the FAIL-OPEN handlers.

**Interfaces:**

- Produces: identical `_is_admin` semantics across all handlers (the contract table). No signature change.

- [ ] **Step 1: Read each handler's current `_is_admin`** (lines noted in the spec) + its `_decode_jwt_payload` (confirm each handler has one; saved_queries/runbooks use `_caller_groups` + a no-bearer→[] path — replace the whole `_is_admin` body with the canonical form, which calls `_decode_jwt_payload`; if saved_queries/runbooks lack `_decode_jwt_payload`, add it by copying the standard one from `api/config/handler.py`).

- [ ] **Step 2: Apply the canonical `_is_admin`** to all 11 handlers verbatim (from the Global Constraints block). For saved_queries/runbooks, this REPLACES the `if not groups: return True` fail-open body entirely — ensure `_decode_jwt_payload` exists in those two (add the standard helper if missing). For the partial handlers (clusters/alerts/approvals/explain/backups), add the `if not claims: return False` line + change the group check. For the gold-standard handlers (config/approval_policies/context_files/onboarding), change only the group-check line from `if "dbops-viewer" in groups and "dbops-admin" not in groups:` to `if groups and "dbops-admin" not in groups:`.

- [ ] **Step 3: Write the contract tests.** For `api/saved_queries` and `api/runbooks` (the FAIL-OPEN ones), create/extend `tests/unit/api/test_saved_queries.py` + `tests/unit/api/test_runbooks.py` with REAL assertions on a WRITE method (PUT or DELETE):

  - no-bearer (raw token, or no auth header) → **403** (regression: this was admin/allowed);
  - `Bearer <garbage-not-jwt>` → 403;
  - `Bearer` + `["dbops-viewer"]` → 403;
  - `Bearer` + `["dbops-analyst"]` (other group) → 403 (the tightening);
  - `Bearer` + no `cognito:groups` claim → admin (proceeds past the gate — assert NOT 403, e.g. 404/400/200 depending on the route);
  - `Bearer` + `["dbops-admin"]` → admin (not 403).
    Mirror the existing test harness in those files. For one already-hardened handler (e.g. `api/config` via `tests/unit/api/test_config.py`), add the `["dbops-analyst"]` other-group → 403 case (the new tightening) — its no-bearer/garbage/viewer cases already exist.

- [ ] **Step 4: Run tests.** `python -m pytest tests/unit/api -q` → PASS. Existing admin/viewer tests across all handlers must still pass (admin still admin, viewer still denied); the new FAIL-OPEN-fix + other-group tests pass.

- [ ] **Step 5: Commit.**

```bash
git add api/*/handler.py tests/unit/api/
git commit -m "fix(admin): unify _is_admin to fail-closed canonical form (close saved_queries/runbooks priv-esc + unknown-group hole)"
```

---

### Task 2: Frontend `isAdmin()` — match the canonical semantics

**Files:**

- Modify: `frontend/src/lib/auth.ts` (`isAdmin()` + its doc comment)

**Interfaces:**

- Consumes: `getUserGroups()` (existing). Produces: `isAdmin()` with option-(b) semantics (cosmetic UI gate; server is authoritative).

- [ ] **Step 1: Update `isAdmin()`.** In `frontend/src/lib/auth.ts`, change the body (currently `if (groups.includes("dbops-viewer") && !groups.includes("dbops-admin")) return false; return true;`) to:

```typescript
const groups = getUserGroups();
// Deny if a group set is present but lacks dbops-admin; empty groups (no
// claim) stays admin (single-admin default). Cosmetic gate only — the
// server enforces.
if (groups.length > 0 && !groups.includes("dbops-admin")) return false;
return true;
```

Update the doc comment above (currently "admin unless explicitly dbops-viewer") to describe the new semantics (admin if no groups or dbops-admin present; otherwise denied).

- [ ] **Step 2: Build.** `cd frontend && npm run build` → PASS, no type errors.

- [ ] **Step 3: Commit.**

```bash
git add frontend/src/lib/auth.ts
git commit -m "fix(admin): frontend isAdmin() matches canonical gate (group set without dbops-admin → not admin)"
```

---

## Post-implementation (controller, after both tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD` — focus: every handler's `_is_admin` is now identical to the canonical form; no handler accidentally diverges; the single-admin (no-group→admin) fallback is preserved; existing admin/viewer behavior unchanged.
- Deploy dev: the handlers span the agent stack (most api/) — `cdk deploy dbops-dev-agent` (+ any stack a changed handler lives in; confirm). Frontend build → sync → CloudFront invalidation `E1234567890ABC`.
- Live smoke (viewer e2e token): a previously-FAIL-OPEN write — `PUT /api/saved-queries/<id>` and a runbooks write — with a RAW (no-Bearer) token → now **403** (was the priv-esc); `Bearer` viewer → 403; confirm a normal admin/GET path still works. (The single-admin no-group case isn't reachable with the viewer token — unit-covered.)
- Then `superpowers:finishing-a-development-branch`.
