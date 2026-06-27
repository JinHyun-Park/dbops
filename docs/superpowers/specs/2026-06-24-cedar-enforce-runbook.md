# Cedar ENFORCE Flip + Async/Conditional Guardrails — Runbook & Design

**Date:** 2026-06-24
**Status:** Deferred by an EXISTING decision — keep LOG_ONLY. This document is the
turnkey revisit procedure for if/when the blocker below is lifted.

> **Prior decision (2026-06-17):** the ENFORCE flip was already investigated and
> **closed with a decision to keep Cedar LOG_ONLY permanently.** Rationale (which
> the 2026-06-24 re-investigation independently re-confirmed): (a) ENFORCE on the
> current permit-all policies adds zero new constraint; (b) per-tool ENFORCE is
> blocked because AgentCore does not expose Cedar decision logs to an observable
> CloudWatch group, so "no legitimate call is denied" cannot be verified, and
> autonomous traffic generation to test it is itself blocked (M2M gateway direct
> call trips the safety classifier; password login is not something an agent may
> do); (c) write control is ALREADY enforced by the tool-level `approval_guard` +
> `execute_sql` classification — a Cedar `forbid` would be redundant
> defense-in-depth, not new protection. This runbook does NOT reopen that
> decision; it captures the exact steps to revisit ONLY if AgentCore Cedar
> observability improves enough to make per-tool ENFORCE evidence-based.

## Why ENFORCE cannot be flipped autonomously

Flipping the gateway binding to ENFORCE blind would risk a default-DENY that
dark-toolizes all ~30 MCP tools (the same catastrophic class as the M2M-creds
outage in project memory) — and the prerequisite evidence to flip safely does not
exist. Three blockers, all requiring live/interactive work:

1. **No Cedar decision logs are being captured.** The CDK comment says Cedar
   "logs every decision to CloudWatch," but a CloudWatch scan on 2026-06-24 found
   **no gateway/policy-engine decision log group** (`/aws/bedrock-agentcore/gateways`,
   `/aws/bedrock-agentcore/policy`, `/aws/vendedlogs/bedrock-agentcore` are all
   empty; only per-runtime log groups exist). Decision-log delivery for the
   policy engine must be configured and confirmed before any ENFORCE decision can
   be evidence-based.
2. **The policies were never validated against the live AgentCore Cedar schema.**
   They are deployed with `validation_mode="IGNORE_ALL_FINDINGS"` precisely
   because the action/context-attribute names are unverified. ENFORCE requires
   confirming each rule matches the auto-generated gateway-target schema.
3. **No real-traffic observation has happened.** Confirming "reads permitted +
   unapproved writes denied" needs live agent sessions exercising the tools
   through the gateway while decisions are logged.

The current state is SAFE: Cedar is LOG_ONLY (never denies), and the real write
enforcement lives in the tool code (`execute_sql.py` SQL classification +
`approval_guard` payload-hash single-use) and the structured-write approval
model. Nothing below weakens that; it only adds a second, gateway-level layer.

## Current state (verified 2026-06-24)

- Policy engine `dbops_dev_policy_engine-6dubd1jle0` is deployed and bound to the
  gateway in `Mode: LOG_ONLY` (`cdk/stacks/agent_stack.py:335,412`).
- Four coarse "permit all tools on the target" policies (one per MCP target:
  performance, incident, simulation, operations), each created one-statement-per
  -policy with `IGNORE_ALL_FINDINGS`.
- The `.cedar` source files in `cdk/policies/cedar/` already contain the STEP-2
  per-tool rules as comments (e.g. `operations_policy.cedar` lists the
  approval-gated write actions in `AgentCore::Action::"<target>___<tool>"` form
  and the PITR force-rule).

## Controlled verification (2026-06-27) — direct control-plane probe

Asked to "directly verify" Cedar before closing the item, we probed the live
engine via `bedrock-agentcore-control` / `bedrock-agentcore` (no traffic, no
binding flip, fully reversible — all probe policies were created with
`enforcementMode=LOG_ONLY` and deleted). Verified facts:

1. **Engine ACTIVE + bound.** `dbops_dev_policy_engine-6dubd1jle0` is `ACTIVE`,
   bound to gateway `dbops-dev-gateway-jrndurhosm` (4 targets: performance /
   operations / incident / simulation).
2. **All 4 policies are ACTIVE with `enforcementMode: ACTIVE`** — definition is
   the coarse permit `permit(principal, action in AgentCore::Action::"<target>",
resource == AgentCore::Gateway::"<gateway-arn>")`.
3. **The enforcement gate is the gateway↔engine BINDING `Mode: LOG_ONLY`, NOT the
   per-policy mode** (which is already `ACTIVE`). So the gateway evaluates + logs
   every decision but never blocks. Flipping enforcement = flipping the _binding_
   mode (`agent_stack.py` `cedar_mode`), not the policies — they are already
   enforce-ready.
4. **No synchronous authorization-test API exists.** `bedrock-agentcore` has only
   `Evaluate` (agent-trajectory evaluation: expectedResponse / assertions /
   toolNames / tokenUsage — NOT Cedar authz); `bedrock-agentcore-control` is
   policy/engine CRUD only. So a decision cannot be probed directly — the only
   ways to observe a DENY are (a) decision logs (absent, blocker #1) or (b) a
   real gateway tool call under ENFORCE.
5. **`CreatePolicy` validation is ASYNC and is the only schema check available.**
   The synchronous call returns `status=CREATING`; the real result lands later as
   `ACTIVE` or `CREATE_FAILED`. Observed: a rule on a **nonexistent** tool action
   (`...___NONEXISTENT_TOOL_XYZ`) went `CREATE_FAILED` (action names ARE validated
   against an auto-generated schema), and a conditional rule
   (`operations___modify_parameter` with `when { context.input.approved == false }`)
   went `ACTIVE` (the STEP-2 conditional form validates). Validation latency is
   real (terminal status can take >40s) — STEP-4 must poll `get-policy` status,
   not trust the create response.

**Conclusion (reinforces the LOG_ONLY-permanent decision):** Cedar is real,
deployed, with ACTIVE enforce-ready policies, gated to LOG_ONLY at the binding.
The engine state is now directly verified, but the one thing that proves runtime
enforcement — observing a gateway DENY — still requires the ENFORCE binding flip
(Part 1), which remains risky without decision-log observability and is redundant
with the tool-level `approval_guard` that already enforces writes. Per-tool
STEP-2 rules validate (with async polling), so STEP-4 is mechanically ready; the
gate is still blocker #1 (decision logs), unchanged since 2026-06-24.

## Part 1 — ENFORCE flip procedure (turnkey for the live session)

Run these in order; do NOT skip the evidence gate (step 3).

1. **Enable Cedar decision-log delivery.** Configure CloudWatch log delivery for
   `dbops_{ENV}_policy_engine` (AgentCore policy-engine logging config). Verify a
   log group appears and receives a decision record after one gateway tool call.
   _(This may require a CDK change to the policy-engine/gateway binding — treat it
   as its own small, reviewed increment, deployed while STILL in LOG_ONLY so it
   cannot deny.)_
2. **Generate real traffic.** Run live agent chat sessions that exercise each MCP
   target's tools — at minimum: a read on each server (performance/incident/
   simulation/operations) and one approved write through the operations approval
   loop. The AgentCore Runtime is ~10 min warm after any deploy, so allow for that.
3. **EVIDENCE GATE — review the decision logs.** Confirm, from the captured
   decisions: (a) every legitimate READ was `ALLOW`; (b) unapproved writes are
   `DENY` (or would be, once the coarse permit is replaced); (c) no false-DENY on
   any real call. If any legitimate call shows DENY, the policy/schema is wrong —
   fix the `.cedar` rule and return to step 2. Do NOT proceed until clean.
4. **Replace the coarse permits with per-tool rules.** In each `cdk/policies/cedar/
*.cedar`, swap the single `permit(... action in AgentCore::Action::"__TARGET__" ...)`
   for the per-tool `permit`/`forbid` rules already drafted in the file comments.
   Verify each `context.input.<param>` referenced is a REAL declared tool parameter
   (`aws bedrock-agentcore-control list-gateway-targets` → the auto-generated
   schema). `execute_sql` stays UN-gated by Cedar (SQL-content decisions live in
   the tool).
5. **Flip the mode + tighten validation.** In `cdk/stacks/agent_stack.py`:
   `cedar_mode = "ENFORCE"` (line ~335) and `validation_mode="FAIL_ON_ANY_FINDINGS"`
   (line ~400). The stricter validation will now REJECT any rule whose
   action/context attrs don't match the schema — that is the point (it proves the
   rules are real before they can deny).
6. **Deploy + live-validate.** `cdk deploy dbops-dev-agent`, wait for warm
   container, then re-run the step-2 traffic: confirm reads still work, an
   unapproved write is now DENIED at the gateway (not just the tool), and an
   approved write passes. If anything legitimate breaks, immediately revert
   `cedar_mode` to `LOG_ONLY` and redeploy (the safe state), then debug.

**Rollback:** set `cedar_mode = "LOG_ONLY"` + `validation_mode="IGNORE_ALL_FINDINGS"`
and `cdk deploy dbops-dev-agent`. Returns to the current never-deny state.

## Part 2 — Async/conditional guardrails (design, build AFTER ENFORCE is stable)

These extend the model only once ENFORCE is proven; building them while dark adds
unvalidated complexity.

- **Async / scheduled / event-triggered tasks.** Today Cedar gates synchronous
  tool calls through the gateway. The agent-tasks subsystem (scheduled +
  event-driven, via the agent-tasks DDB stream → task_worker) must be checked:
  determine whether its tool calls traverse the SAME gateway (then they are
  already covered once ENFORCE is on) or call MCP tools out-of-band (then they
  need an equivalent `approval_guard`/Cedar check at the worker). Decide per
  action-category: an event-driven auto-RCA is read-only (auto-allow); any async
  WRITE must still require an approval record, exactly like the sync path.
- **Attribute-based (conditional) policies.** Differentiate by resource attribute,
  e.g. stricter rules for `prod` clusters than `dev`. Cedar can express this with
  `when { context.input.<attr> == ... }`, BUT the attribute must be a real,
  schema-declared tool parameter that the tool actually passes. So this requires:
  (a) tools to include the discriminating attribute (e.g. an `environment` or
  cluster-tier field) in their input, (b) the gateway schema to surface it, then
  (c) a `forbid ... unless approved` rule keyed on it. Sequence: add the attribute
  to the tool contracts first, observe it in LOG_ONLY decisions, then enforce.

## Acceptance criteria (for the eventual live session)

- A Cedar decision log group exists and shows ALLOW for reads, DENY for unapproved
  writes, zero false-DENY across all four targets.
- `cedar_mode == "ENFORCE"`, `validation_mode == "FAIL_ON_ANY_FINDINGS"`, deploy
  succeeds (proving all rules schema-valid).
- Live: read works; unapproved write DENIED at gateway; approved write passes;
  full unit suite + `tests/cdk/test_synth.py` green.

## References

- `cdk/stacks/agent_stack.py:316-430` (Cedar engine + policy creation + binding)
- `cdk/policies/cedar/*.cedar` (policies; STEP-2 per-tool rules in comments)
- Project memory: Cedar automation gotchas (engine schema, 3-underscore action
  form, single-statement-per-policy, role GetPolicyEngine + AuthorizeAction).
