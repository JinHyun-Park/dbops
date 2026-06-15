# AgentCore Cedar Policies

Cedar policies define authorization rules enforced at the AgentCore Gateway
level. They are evaluated outside agent/tool code — the agent cannot bypass
them. They are a **defense-in-depth outer gate**; the authoritative write
control is the tool-level `approval_guard` (payload-hash + atomic single-use)
in `mcp-servers/mcp_servers/operations/tools/`.

## Policy Files

- `cedar/performance_policy.cedar` — READ-ONLY: permit all tools on the target
- `cedar/incident_policy.cedar` — READ-ONLY: permit all tools on the target
- `cedar/simulation_policy.cedar` — READ-ONLY: permit all tools on the target
- `cedar/operations_policy.cedar` — MIXED (coarse permit for the LOG_ONLY
  rollout; see the in-file STEP 2 comment for the per-tool ENFORCE refinement)

## How Policies Are Applied (automated — `cdk deploy`)

`cdk/stacks/agent_stack.py` deploys these automatically as part of the agent
stack — **no manual `agentcore policy create` step**. For each `.cedar` file it:

1. Creates one `AWS::BedrockAgentCore::PolicyEngine` (`dbops-<env>-policy-engine`).
2. Strips `//` comments, splits the file into individual statements (AgentCore
   `CreatePolicy` accepts exactly ONE statement per policy), and creates one
   `AWS::BedrockAgentCore::Policy` per statement.
3. Substitutes the `__TARGET__` placeholder with the env-specific gateway target
   name and binds every `resource` to the concrete gateway ARN.
4. Binds the engine to the Gateway via `PolicyEngineConfiguration`.

## AgentCore Cedar schema (gotchas that bit us)

- **Policy name** must match `^[A-Za-z][A-Za-z0-9_]*$` — underscores only.
- **One statement per policy** — a multi-statement file fails ("unexpected
  token forbid"). The CDK splits them.
- **Resource** must be a _specific_ gateway for constrained-action policies:
  `resource == AgentCore::Gateway::"<gateway-arn>"` (not a wildcard or the bare
  type). The CDK injects the ARN.
- **Action** is target-scoped: `action in AgentCore::Action::"<target>"` permits
  all tools on a target; a single tool is
  `AgentCore::Action::"<target>___<tool>"` (THREE underscores). Bare tool names
  are rejected ("Target '<tool>' does not exist").
- **`context.input.<param>`** is validated against the tool's _real_ declared
  parameters (auto-generated schema). Reference only params the tool actually
  has; guard optional ones with `context.input has <param> && ...`.
- Cedar has **no** `toUpper` / `startsWith` / `contains` — only the
  case-SENSITIVE `like` operator. SQL-content rules (SELECT auto / DROP-TRUNCATE
  force) therefore live in the tool code (`execute_sql.py`), not in Cedar.
- The Gateway role needs `bedrock-agentcore:GetPolicyEngine` (on the engine ARN)
  and `AuthorizeAction` / `PartiallyAuthorizeActions` (on the gateway ARN).

## Rollout mode: LOG_ONLY → ENFORCE

`CEDAR_MODE` in `agent_stack.py` is **`LOG_ONLY`**: Cedar evaluates and logs
every decision to CloudWatch but never DENIES. This was chosen because these
policies had never been validated against the live gateway. To enforce:

1. Observe the CloudWatch decision logs against real agent tool-calls — confirm
   reads are permitted and the intended writes/denies evaluate correctly.
2. Add the per-tool write conditions to `operations_policy.cedar` (see its STEP
   2 comment), verifying each `context.input.<param>` via
   `aws bedrock-agentcore-control list-gateway-targets`.
3. Flip `CEDAR_MODE` to `"ENFORCE"` and the policies' `validation_mode` to
   `"FAIL_ON_ANY_FINDINGS"`, then `cdk deploy`.

Default posture is DENY (only explicitly permitted actions are allowed) and
`forbid` rules override `permit` rules — but only once `mode = ENFORCE`.
