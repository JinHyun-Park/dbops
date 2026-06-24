# Multi-Team Tenancy T-4 — Agent/Chat SSE Tenancy — Design

**Date:** 2026-06-24
**Status:** approved-direction (final increment of the multi-team-tenancy program; T-1/T-2/T-3 shipped. User delegated the direction; this is the highest-risk increment — the chat agent is the only remaining platform-wide surface.)

## Context

T-1..T-3 made every REST cluster-read tenant-scoped and shipped the admin Teams
UI. The **chat agent remains platform-wide**: `agent/server.py`'s
`invoke(payload, context)` runs with no caller identity and the agent can call
any MCP tool with any `cluster_id` (the cluster comes from the `[cluster: id]`
prefix the frontend prepends + the agent's own reasoning). A determined
viewer-team-member could ask the agent about a cluster they can't see in the UI
and the agent would answer. T-4 closes this.

### Grounded facts (verified)

- **Identity is available to the agent.** The Runtime is configured with a
  Cognito JWT inbound authorizer (`agent_stack.py:562`
  `RuntimeAuthorizerConfiguration.using_cognito(user_pool, user_pool_clients)`).
  The SDK forwards the inbound `Authorization` header into the request context:
  `bedrock_agentcore/runtime/app.py:321-324` explicitly copies the
  `Authorization` header into `context.request_headers`, and the entrypoint
  receives it as `invoke(payload, context)` → `context.request_headers`
  (RequestContext has `request_headers: Optional[Dict[str,str]]`,
  `runtime/context.py:16`). So the agent can read the SAME Cognito ID token the
  frontend sent (already validated by AgentCore) → `cognito:username` +
  `cognito:groups`.
- **Enforcement hook exists.** Strands exposes `BeforeToolInvocationEvent` /
  `BeforeToolCallEvent` via `HookProvider`/`HookRegistry`
  (`strands/experimental/hooks/`). A hook can inspect a tool call's arguments
  (the `cluster_id`) BEFORE the MCP tool executes and abort it — the hard gate.
- **The cluster reaches tools as a tool argument.** MCP tools take `cluster_id`
  in their arguments; the agent fills it from the prompt. There is no
  server-side check today (`agent/server.py:331` builds the Agent with all
  Gateway tools; `:333` streams).
- **The overlay already exists** as the vendored `tenancy.py`
  (`is_admin`, `caller_username`, `my_team_ids`, `visible_cluster_ids`,
  `visible_set_from_registry`) — T-1/T-2. The agent needs its own copy + the
  table env + IAM (the Runtime currently has neither `CLUSTERS_TABLE` nor
  `TEAM_MEMBERS_TABLE` in its env, and its IAM role can't read them).

## Architecture — two-layer enforcement

**Identity → visible set (per invoke):** In `invoke`, read
`context.request_headers.get("Authorization")` → decode the JWT (vendored
tenancy helpers) → if `is_admin` → unrestricted; else resolve the visible
cluster_id set via the registry scan (`visible_set_from_registry`-equivalent,
using the agent's `CLUSTERS_TABLE`/`TEAM_MEMBERS_TABLE` + the `by-user` GSI).

**Layer 1 — system-prompt constraint (LLM-level guard):** when the caller is
NOT an admin, inject the visible cluster set into the system prompt: an explicit
instruction listing the allowed cluster_ids and directing the agent to refuse
any request targeting a cluster not in the list (with a Korean refusal). This is
advisory (the model could be talked around it) but is the first line and gives a
clean UX (the agent explains it can't access that cluster).

**Layer 2 — `BeforeToolInvocation` hook (hard gate):** register a Strands hook
that, before ANY tool executes, extracts a `cluster_id` argument from the tool
call and — if the caller is non-admin and the cluster_id is not in the visible
set — ABORTS the tool with an error result ("이 클러스터에 대한 접근 권한이
없습니다."). Tools without a `cluster_id` argument (AWS doc tools, fleet-wide
tools) pass through. This is the guarantee that survives prompt manipulation:
even if the LLM tries to call a tool on a hidden cluster, the hook blocks the
execution before it touches data.

**Admin:** `is_admin` → no system-prompt restriction + the hook is a no-op
(visible set is "all"). Identical to today for admins.

### Fail behavior

- **No/undecodable Authorization** (header absent, decode fails): treat as a
  non-admin with NO teams ⇒ visible set = unassigned clusters only (mirrors the
  REST viewer-no-teams default; default-open for unassigned preserves chat about
  unassigned clusters, which is today's behavior for a zero-teams deployment).
  This is fail-toward-restriction for assigned clusters, fail-open for
  unassigned — consistent with the REST layers.
- **Registry/membership scan error:** fail-open to current behavior (visible set
  resolution returns "all" / None) — do not break chat on a transient DDB
  outage. Consistent with the T-1 fleet filter + T-2 `visible_set_from_registry`.
  (The hook, given an "all" set, is a no-op — chat keeps working; the isolation
  degrades open during the outage, same tradeoff the REST layers accept.)

## Data flow

Browser chat → SSE invoke (Cognito token in `Authorization`, `[cluster: id]` in
prompt) → AgentCore validates the token (Cognito authorizer) + forwards it to
the agent → `invoke` decodes it → resolves the visible set → builds the system
prompt WITH the visible-cluster constraint (non-admin) + registers the
`BeforeToolInvocation` gate hook → the agent streams; any tool call on a
non-visible cluster is blocked by the hook before execution.

## Components

1. **`agent/tenancy.py`** (vendored copy of the overlay; agent/ can't import
   api/ — same byte-identical pattern, but the agent's copy may differ
   structurally since it's a different package root, so it is NOT in the api/
   parity set — instead the agent copy gets its OWN focused unit test). Provides
   `is_admin_claims`, `caller_from_headers(headers)`, `visible_cluster_ids_for(headers)`
   returning `None` (admin/all) or a `set`.
2. **`agent/server.py`** — in `invoke`: resolve `visible = visible_cluster_ids_for(context.request_headers)`;
   pass it to `build_system_prompt` (constraint text when not None); register the
   `BeforeToolInvocation` hook bound to `visible`.
3. **`agent/prompts/system_prompt.py`** — `build_system_prompt(context_files, visible_clusters=None)`
   appends the visibility constraint block when `visible_clusters` is not None.
4. **A tool-gate hook** (`agent/tool_gate.py` or inline) implementing
   `HookProvider` — on `BeforeToolInvocationEvent`, extract `cluster_id` from the
   tool arguments; abort if not visible.
5. **CDK (`agent_stack.py`)** — add `CLUSTERS_TABLE`, `TEAM_MEMBERS_TABLE`,
   `TEAM_MEMBERS_BY_USER_INDEX` to the Runtime `environment_variables`; grant the
   Runtime's IAM role read on `clusters_table` + `team_members_table` (+ the GSI).

## Error handling

- All identity/visible-set resolution wrapped so a failure never crashes
  `invoke` — on error, fall back per "Fail behavior" (unassigned-only for a
  decode failure; all/None for a registry-scan failure) and log
  `type(e).__name__` (no token/PII in logs).
- The hook must never raise an unhandled exception into the stream — on its own
  internal error it should fail-closed for non-admin assigned clusters (block)
  but must return a clean tool-error, not crash the turn.
- **No `__pycache__`** in `agent/` (AgentCore rejects bytecode artifacts) —
  validate `.py` via `ast.parse`, never `py_compile`/exec; the CDK artifact
  already excludes `**/__pycache__`.

## Testing

- **`agent/tenancy.py` unit** (`tests/unit/agent/test_agent_tenancy.py`):
  admin headers → None; viewer-in-team-A → unassigned ∪ team-A; viewer-no-teams
  → unassigned only; no/undecodable Authorization → unassigned-only;
  registry-scan error → None (fail-open). (Mock boto3 DynamoDB.)
- **tool-gate hook unit:** a tool call with a non-visible `cluster_id` →
  aborted; a visible cluster_id → allowed; a tool with no `cluster_id` arg →
  allowed; admin (visible None) → always allowed.
- **system_prompt unit:** `build_system_prompt(..., visible_clusters={...})`
  contains the constraint + the cluster ids; `visible_clusters=None` (admin) →
  no constraint block (backward-identical).
- **`agent/server.py`**: validate via `ast.parse` (no import-time AWS);
  the existing agent tests still pass.
- CDK synth green (the new env + grants).

## Security

- The hard boundary is the `BeforeToolInvocation` hook (survives prompt
  injection); the system-prompt constraint is UX + first-line.
- Admin unchanged (sees all). Default-open: unassigned clusters remain
  chat-accessible (zero-teams = today). Assigned clusters are blocked for
  non-members at the tool layer.
- The token is read from the already-validated inbound header; never logged.
- **Coverage honesty:** this closes the agent/chat surface. Combined with
  T-1/T-2 (REST) + T-3 (UI), the multi-team-tenancy program is then complete:
  every cluster-data surface — REST reads, the fleet view, and the chat agent —
  is tenant-scoped, default-open, admin-unrestricted.

## Out of scope

- Cedar/Gateway-level per-tool enforcement (Cedar is LOG_ONLY by decision — the
  in-agent hook is the chosen enforcement point; Cedar remains a future
  defense-in-depth option).
- Per-cluster prompt-history isolation in AgentCore Memory (session-scoped, not
  cluster-scoped — out of scope, like the REST `memory` handler).
