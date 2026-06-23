# Advanced Approval — Designated Approvers — Design

**Date:** 2026-06-23
**Status:** approved

## Problem

Today any `dbops-admin` can approve any pending operation
(`api/approvals/handler.py` PUT, gated only by `_is_admin`). There is no way to
require that a high-risk change on a specific cluster be approved by a specific
person, and nothing prevents an admin from approving their own request
(no separation of duties). For multi-team fleet operation this is too coarse.

## Goal

Let an admin define **approval policies** that route approval rights for
specific clusters/actions to **designated approvers**, and prevent
self-approval. When no policy matches a request, behavior is unchanged (any
admin may approve) — so existing deploys keep working until policies are added.

Non-goals: per-request ad-hoc approver lists; Cognito-group-based approvers
(named users only); capturing the human who triggered an agent action
(`requested_by` stays "agent" for agent requests — see Self-approval below);
multi-stage / N-of-M approvals.

## Architecture

A small **policy store** in DynamoDB, a pure **matching function** that resolves
the eligible approver set for a request, **enforcement** inside the existing
approve flow, and an **admin-only management UI + API** to CRUD policies.

Enforcement is **additive with fallback**: a matching policy narrows approval to
(admin AND in the designated set AND ≠ requester); no matching policy falls back
to the current "any admin" rule. Self-approval is **always** prevented.

### Components

1. **`dbops-{env}-approval-policies` DynamoDB table** (FoundationStack)

   - PK `policy_id` (S, generated UUID). Attributes: `cluster_id` (S — an exact
     cluster id or `"*"`), `action_type` (S — an exact action_type/tool_name or
     `"*"`), `approvers` (list of S — emails/usernames), `description` (S),
     `updated_at` (S, ISO-UTC), `updated_by` (S).
   - `PAY_PER_REQUEST`, PITR on, `DESTROY` removal — matches sibling tables.
   - Lives in foundation so both the policy API (agent stack) and the approvals
     API (agent stack) reach it via grant helpers; no cross-stack cycle.
   - Grant helpers: `grant_approval_policy_read(fn)` / `grant_approval_policy_write(fn)`
     (each sets `APPROVAL_POLICIES_TABLE` env + read or read/write grant).

2. **Matching function** — pure, in `api/approvals/handler.py` (the enforcement
   point) so it is unit-testable without AWS:
   `resolve_eligible_approvers(cluster_id, action_type, policies) -> set[str]`

   - A policy matches when `(p.cluster_id in (cluster_id, "*"))` AND
     `(p.action_type in (action_type, "*"))`.
   - Specificity score = `(2 if p.cluster_id == cluster_id else 0) + (1 if p.action_type == action_type else 0)`.
   - Result = approvers of the matching policy(ies) with the **highest** score;
     ties at the top score → **union** of their approvers.
   - No match → **empty set** (policy not applicable → fallback).
   - Approver matching is case-insensitive on the stored strings and is compared
     against the caller's token identity (preferred_username / cognito:username /
     email — the same fields `_caller_name` already resolves).

3. **Enforcement** — `api/approvals/handler.py` PUT approve, after the existing
   `_is_admin` check and before the pending→approved transition:

   - `approver = _caller_name(event)` (from the verified token, never the body).
   - **Self-approval:** if `approver == item["requested_by"]` → `403`
     ("자기 요청은 승인할 수 없습니다"). (Bites for human/UI-created requests;
     agent requests carry `requested_by="agent"`, so no human collision.)
   - **Designated approver:** load policies, `eligible = resolve_eligible_approvers(...)`.
     If `eligible` is non-empty (a policy matched) and `approver` (normalized)
     is not in it → `403` ("이 작업은 지정된 승인자만 승인할 수 있습니다").
   - Empty `eligible` (no policy) → proceed (current behavior).
   - **Both new checks apply to `action == "approve"` only.** `reject` keeps the
     existing `_is_admin`-only gate (any admin may reject; a requester may reject
     /cancel their own request — only _granting_ approval is restricted and
     self-approval-protected).
   - Wiring: `foundation.grant_approval_policy_read(approvals_lambda)` (table env + read).

4. **Policy CRUD API** — `api/approval_policies/handler.py`, new `ApprovalPoliciesApi`
   Lambda in AgentStack, routes `GET/POST /api/approval-policies` and
   `PUT/DELETE /api/approval-policies/{id}`.

   - **Admin-gated + fail-closed** `_is_admin` (copy the hardened
     `api/config/handler.py` form: no `Bearer ` → False; empty/garbage claims →
     False; viewer → 403). Viewer → 403 on every method.
   - Validation: `cluster_id` and `action_type` non-empty strings (default `"*"`);
     `approvers` a non-empty list of non-empty strings (trimmed, lower-cased for
     storage/compare); `description` optional string. Bad input → `400`.
   - `GET` lists all policies; `POST` creates (generates `policy_id`); `PUT`
     updates by id; `DELETE` removes by id. `updated_by` stamped from the token.
   - OpenAPI: regenerate `frontend/public/openapi.json`; `test_openapi_spec.py` parity.

5. **Admin management UI** — `frontend/src/app/approval-policies/page.tsx`,
   admin-only, mirroring the Settings page shell + the hardened gating.
   - Nav entry under "Configure" with `adminOnly: true` → hidden from viewers in
     the sidebar AND the ⌘K command palette (`isAdmin()` gate). A viewer who
     navigates directly sees an "관리자 전용" notice (the API also returns 403).
   - Lists policies; add/edit/delete with fields cluster_id, action_type,
     approvers (comma/line list), description. Helper copy (Korean) explains
     wildcards (`*`), most-specific-wins, additive+fallback, and that
     `action_type` matches the approval request's action_type/tool_name.
   - New `api-client.ts` functions: `fetchApprovalPolicies`,
     `createApprovalPolicy`, `updateApprovalPolicy`, `deleteApprovalPolicy`.

## Data Flow

- **Manage:** admin → `/approval-policies` UI → CRUD API → policies table.
- **Enforce:** admin clicks Approve → `PUT /api/approvals/{id}` → handler loads
  policies (paginated scan; table is small) → `resolve_eligible_approvers` →
  self-approval + designated checks → existing pending→approved transition.
- **Fallback:** no policy / policy-read error → eligible empty → any admin (the
  pre-feature behavior), with self-approval still enforced.

## Error Handling

- API: invalid policy input → `400` (nothing written). Non-admin → `403`.
- Enforcement: a policy-table read failure is swallowed → `eligible` empty →
  fallback to any-admin (fail-SAFE — a policy-infra outage must not freeze the
  approval loop). Self-approval prevention does not depend on the table, so it
  still applies. Log the read failure once.
- The pending→approved `ConditionExpression` (replay/idempotency guard) is
  unchanged and still runs after the new checks pass.

## Testing

- **Matching function** (pure): exact cluster+action wins over wildcards;
  wildcard-only matches; tie at top specificity → union; no match → empty.
- **Enforcement:** self-approval → 403; matched policy + non-designated approver
  → 403; matched policy + designated approver → 200; no policy → any admin 200;
  policy-read error → fallback 200 (+ self-approval still 403).
- **Policy CRUD:** admin gate (viewer 403 on every method), fail-closed
  (no-bearer / garbage token → 403), input validation (empty approvers → 400),
  create/update/delete round-trip; `test_openapi_spec.py` parity.
- **CDK:** synth + table-present assertion.
- **Frontend:** `npm run build` green.

## Security

- Approve is server-side gated: `_is_admin` AND (designated check OR fallback)
  AND not-self. The UI nav gating is cosmetic; the API enforces.
- The policy CRUD API is admin-only and fail-closed (the hardened pattern).
- Policies store usernames/emails only — no secrets.
- Self-approval prevention is a baseline applied to all approvals regardless of
  policy.
