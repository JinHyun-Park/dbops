# Admin-Gate Model Hardening (codebase-wide `_is_admin`) — Design

**Date:** 2026-06-24
**Status:** approved (autonomous — design decisions made by the implementer per the user's "proceed without asking" directive)

## Problem

The admin gate `_is_admin` is copied into 11 API handlers with inconsistent,
partly fail-OPEN logic (surfaced repeatedly across this session's reviews):

- **FAIL-OPEN (real priv-esc):** `api/saved_queries` + `api/runbooks` have
  `if not groups: return True` with NO `Bearer ` guard — a scheme-less or
  bearer-less request decodes to empty groups and is treated as **admin**. These
  are WRITE-gated endpoints (saved-query/runbook PUT/DELETE), so a viewer (or
  anyone) can write by omitting the `Bearer ` prefix.
- **Garbage-token hole:** `clusters`, `alerts`, `approvals`, `explain`,
  `backups` fail-closed on no-bearer but lack the `if not claims: return False`
  guard, so `Bearer <non-jwt>` → empty claims → admin (the gateway 401s most
  such tokens, so this is defense-in-depth).
- **Unknown-group → admin (all 11):** the group check only denies an explicit
  `dbops-viewer`; a token with any OTHER group (e.g. a custom `dbops-analyst`)
  or no group passes as admin.
- The frontend `isAdmin()` (auth.ts) mirrors the same loose semantics.

## Goal

Unify every `_is_admin` (11 handlers) and the frontend `isAdmin()` to one
hardened, fail-closed canonical form that: rejects bearer-less + unparseable
tokens, denies any non-empty group set lacking `dbops-admin` (closing the
unknown-group hole), and PRESERVES the intentional single-admin dev fallback
(a valid token with NO group claim → admin) and `dbops-admin` → admin.

Non-goals: removing the no-group→admin single-admin fallback (foundation does
not auto-create/assign a `dbops-admin` group, so requiring explicit membership
would lock out fresh single-admin deploys — out of scope / would need a
deploy-time group assignment); changing what each endpoint gates (only the
gate's correctness changes); a shared Lambda layer for the helper (handlers
stay independent — the canonical form is copied identically, as today).

## Architecture

The canonical `_is_admin` (Python) applied verbatim to all 11 handlers, and the
matching `isAdmin()` (TypeScript) in the frontend. Behavior table (the contract):

| token                                                                    | result                            |
| ------------------------------------------------------------------------ | --------------------------------- |
| no `Bearer ` prefix                                                      | **deny** (fail-closed)            |
| `Bearer <unparseable>` (empty claims)                                    | **deny**                          |
| valid token, `cognito:groups` absent/`[]`                                | **admin** (single-admin fallback) |
| valid token, groups present without `dbops-admin` (incl. `dbops-viewer`) | **deny**                          |
| valid token, groups include `dbops-admin`                                | **admin**                         |

### Components

1. **Canonical Python `_is_admin`** — applied to `api/{saved_queries, runbooks,
clusters, alerts, approvals, explain, backups, config, approval_policies,
context_files, onboarding}/handler.py`:

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
       # Deny any authenticated principal whose group set lacks dbops-admin.
       # Empty/absent groups stays admin (single-admin dev fallback).
       if groups and "dbops-admin" not in groups:
           return False
       return True
   ```

   Each handler keeps its own `_decode_jwt_payload` (already present). Only the
   `_is_admin` body changes; the bodies were structurally similar, so the diff
   per handler is small. Handlers that already match (config, approval_policies,
   context_files, onboarding differ only in the final group-check line) get just
   the group-check line changed to the `if groups and ...` form.

2. **Frontend `isAdmin()`** — `frontend/src/lib/auth.ts`:

   ```typescript
   export function isAdmin(): boolean {
     const groups = getUserGroups();
     // Deny if a group set is present but lacks dbops-admin; empty groups
     // (no claim) stays admin (single-admin default). Cosmetic gate only —
     // the server enforces.
     if (groups.length > 0 && !groups.includes("dbops-admin")) return false;
     return true;
   }
   ```

   Update the doc comment above it to match (currently says "admin unless
   explicitly dbops-viewer").

## Data Flow

Unchanged — `_is_admin` is called at the top of each handler's write/mutating
paths (and GET on the admin-only ones). Only the boolean result changes for the
previously-mis-classified cases (bearer-less, garbage, unknown-group).

## Error Handling

`_is_admin` returns a bool, never raises (the `_decode_jwt_payload` is already
exception-safe). Denied → the handler's existing `403`.

## Testing

- **Per representative handler** (at minimum the two FAIL-OPEN ones —
  `saved_queries`, `runbooks` — plus one already-hardened one as a regression
  guard): a parametrized set asserting the contract table — no-bearer → deny,
  `Bearer <garbage>` → deny, viewer → deny, other-group (`dbops-analyst`) →
  deny, no-group → admin, `dbops-admin` → admin. For the FAIL-OPEN handlers,
  explicitly assert a no-bearer WRITE (PUT/DELETE) is now 403 (was the priv-esc).
- The existing `_is_admin` tests across handlers (which use `Bearer` + admin/
  viewer tokens) must still pass — admin still admin, viewer still denied.
- Full unit suite green; `npm run build` for the frontend.

## Security

- Closes two real fail-OPEN priv-escs (`saved_queries`, `runbooks` write via
  bearer-less request) and the garbage-token + unknown-group holes everywhere.
- Preserves the documented single-admin dev fallback (no-group → admin) so fresh
  deploys are not locked out.
- Frontend change is cosmetic (UI gating); the server is authoritative.
