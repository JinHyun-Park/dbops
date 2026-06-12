# NoSQL write / remediation tools — Design Spec

- **Date**: 2026-06-12
- **Status**: Accepted (Codex adversarial safety review → FIX-FIRST; all 7 holes folded in)
- **Program**: Multi-engine #P3.6 Group C — the FINAL remaining item (write/remediation).
- **Decision basis**: Explore audit of the operations write/approval/Cedar infra +
  the two read-only NoSQL features already shipped (cost simulator, Mongo collector).

## Goal

Give the agent **approval-gated** write/remediation actions for DynamoDB and
DocumentDB, mirroring the EXACT operations-server safety model (Cedar
`approved==true` at the Gateway + `approval_guard` payload-hash/atomic-consume at
the tool). Every action is a no-op until a DBA approves it in the Approval Center.
These let the agent _execute_ what the read-only features already _recommend_
(the DynamoDB cost simulator's mode switch; the Mongo collector's "enable profiler").

## Scope (user-confirmed)

**DynamoDB** (boto3 `dynamodb` client via `client_for_cluster`, cross-account aware):

1. `modify_dynamodb_capacity` — `update_table`: set provisioned RCU/WCU **and/or**
   switch billing mode (Provisioned↔On-Demand).
2. `modify_dynamodb_ttl` — `update_time_to_live`: enable/disable an attribute TTL.
3. `enable_dynamodb_pitr` — `update_continuous_backups`: turn PITR on (or off).

**DocumentDB** (Mongo wire protocol, in-VPC pymongo — a bounded WRITE allowlist): 4. `set_docdb_profiler` — `db.setProfilingLevel(level, {slowms})` (execute what the
`docdb_mongo_profiler_off` finding recommends). 5. `create_docdb_index` — `db.<coll>.createIndex(keys, {background:true, name})`.

All five are WRITE → approval-gated. NO generic runCommand/eval; the Mongo writes
are a hardcoded two-command allowlist, exactly as the read collector is for reads.

## Where the tools live: the existing `operations` MCP server

Add all five to the operations server (NOT a new server). Rationale: the whole
approval machinery is bound there — `request_approval`'s `action_type` enum, the
`approval_guard._project()` per-action payload projections, `operations_policy.cedar`,
and the parity test. A new server would fork all of that. The operations Lambda is
already in-VPC (`vpc=data.vpc`), so it can reach DocDB 27017; it only lacks pymongo.

### Engine gating (POSITIVE, per the simulation precedent)

Add to `CAPABILITIES` (all copies — `mcp-servers/.../shared/engine_family.py`,
`api/clusters/engine_family.py`, `api/dashboard/engine_family.py`,
`data-pipeline/.../engine_family.py`; verify byte-identical CAPABILITIES block by md5):

- `dynamodb`: `"ddb_write": True`
- `documentdb`: `"docdb_write": True`

The operations handler today has NO engine guard (accepts any cluster — fine for
Aurora SQL). Add a **per-tool** positive gate ONLY for these five (leave the Aurora
tools ungated): resolve family, and for each NoSQL tool require its capability key,
else return `{"status":"unsupported_engine", ...}`. A DynamoDB tool called on an
Aurora cluster must refuse; an Aurora tool on a DynamoDB cluster is out of scope
here (those stay as-is — `execute_sql` etc. are already Aurora-shaped).

**FAIL-CLOSED for writes (review fix #3):** the simulation read tool DEFAULT-PERMITs
on `family=None` (missing row / lookup error / empty cluster_id) — acceptable for a
read. These are WRITES, so the opposite rule applies: **if the family or its
capability cannot be resolved, REFUSE**. `_resolve_family` returning None → return
`{"status":"unsupported_engine","reason":"cluster engine could not be resolved"}` and
do not execute. An unknown/unregistered/lookup-failed cluster must NEVER slip a write
through, even with a valid-looking approval.

## The approval round-trip (unchanged contract — we only extend the tables)

Each write tool follows the established 3-state shape verbatim:

1. Called without `approved=true` → `{"status":"approval_required", <echo args>}`.
2. Agent calls `request_approval(cluster_id, action_type, action_details=<exact args>)`.
3. DBA approves on `/approvals` → REST flips `approved`, stamps `resolved_at`.
4. Agent re-calls the tool with `approved=true, approval_id=<uuid>`.
5. `verify_approval(approval_id, cluster_id, action_type, payload=<exact args>)` →
   on `{"ok":True}` execute; else `{"status":"approval_denied", "reason":...}`.

### `request_approval` enum (operations/tools/request_approval.py)

Add the five new `action_type` values to the allowed enum:
`modify_dynamodb_capacity`, `modify_dynamodb_ttl`, `enable_dynamodb_pitr`,
`set_docdb_profiler`, `create_docdb_index`.

### `approval_guard._project()` — new payload projections (exact binding)

Each binds the approval to WHAT executes (so an approval can't be reused for a
different change). The hash must cover **every field that changes the executed call**
AND the **normalized/effective values actually sent to AWS** (review fixes #1, #2, #4):

- `modify_dynamodb_capacity` → `{"target": <table identity>, "billing_mode": str|"",
"rcu": int|None, "wcu": int|None, "force": bool}` — `rcu`/`wcu` are the EFFECTIVE
  values after the floor (see #4 below: reject `<1` rather than silently flooring, so
  the hashed value == the executed value). Include the table target even though a
  `ddb-*` cluster_id is 1:1 with a table (`verify_approval` already checks
  `row.cluster_id == cluster_id`) — bind it explicitly so no user-controllable target
  field is ever outside the hash (#1).
- `modify_dynamodb_ttl` → `{"attribute": str, "enabled": bool}`
- `enable_dynamodb_pitr` → `{"enabled": bool, "force": bool}` (force required to DISABLE — #7)
- `set_docdb_profiler` → `{"db": str, "level": int, "slowms": _norm_val}`
- `create_docdb_index` → `{"db": str, "collection": str,
"keys": [[field, direction], ...], "name": str}` — **keys is an ORDERED list of
  `[field, direction]` pairs, NOT a sorted dict** (review fix #2). Compound-index field
  order is semantically significant; sorting it would both build the wrong index and
  hash a different index than Mongo executes. Hash and execute the exact same ordered
  list (`create_index(list-of-tuples)`).

`_norm_val` still collapses numeric aliases (`2`/`2.0`/`"2"`) so the request-time and
execute-time hashes match.

## DynamoDB tools — detail

`client = client_for_cluster(cluster_id, "dynamodb")` (hub-spoke). Two distinct read
phases: a **request-time** `describe_*` to surface AWS constraints as
`approval_required` warnings, AND — critically — an **execute-time re-read
immediately before the write** to defeat TOCTOU (review fix #6). The approval binds an
**expected current-state precondition**; if the re-read shows the table drifted from
it (mode already changed, PITR already toggled, GSIs added), ABORT with
`{"status":"approval_denied","reason":"table state changed since approval"}` rather
than executing a now-different effective change.

- **modify_dynamodb_capacity**: `update_table(BillingMode=..., ProvisionedThroughput=...)`.
  - **Reject `<1` RCU/WCU rather than silently flooring** (review fix #4): if the
    caller asks for `<1`, return `approval_denied`/`error` — never normalize _after_
    hashing, or the executed value would differ from the approved one. The projection
    hashes the effective (already-validated ≥1) value.
  - Switching TO Provisioned **requires** RCU/WCU; switching TO On-Demand drops them.
  - **GSIs (review fix #5):** GSIs do NOT inherit provisioned throughput — each needs
    its own. **v1 BLOCKS a capacity change on a table that has any GSI**: the
    request-time describe detects GSIs and returns `{"status":"unsupported",
"reason":"per-GSI capacity not supported in v1; change via Console/CDK"}`. (Per-GSI
    capacity is a documented v2 follow-up.)
  - A billing-mode switch is **rate-limited by AWS** — surface in the warning; on
    `LimitExceededException` return a clean `{"status":"error", reason}` (never crash).
- **modify_dynamodb_ttl**: `update_time_to_live(TimeToLiveSpecification={Enabled, AttributeName})`.
  One TTL change per ~1h — surface; idempotent if already in state.
- **enable_dynamodb_pitr**: `update_continuous_backups(PointInTimeRecoverySpecification=
{PointInTimeRecoveryEnabled: bool})`. Idempotent. **DISABLING PITR is a
  data-protection degradation — requires `force=true`** (hashed; see Cedar #7).

IAM on `operations_mcp_lambda` (agent_stack): add `dynamodb:UpdateTable`,
`dynamodb:UpdateTimeToLive`, `dynamodb:UpdateContinuousBackups`, `dynamodb:DescribeTable`,
`dynamodb:DescribeContinuousBackups`, `dynamodb:DescribeTimeToLive` (resource `*` —
cross-account is via assume-role, already granted `sts:AssumeRole`).

## DocumentDB Mongo-write tools — detail

The operations Lambda is in-VPC but has no pymongo. **Bundle pymongo + the RDS/DocDB
CA into the operations asset** via `BundlingOptions`, reusing the `_PipLocalBundling`
class already written for the Mongo collector (Docker-free local pip, Docker fallback).
This makes pymongo available to the operations handler (the other MCP servers share the
same `../mcp-servers` asset — they get pymongo too; harmless, unused).

**Separate WRITE credentials (critical):** the read collector uses a least-privilege
**read-only** Mongo user (`mongo_secret_arn`). Profiler/index writes need write
privileges, so add a SECOND registry field **`mongo_write_secret_arn`** pointing at a
distinct, scoped **read-write** Mongo user's secret. A documentdb cluster without
`mongo_write_secret_arn` → the write tools return `{"status":"unsupported_engine",
"reason":"no write credentials configured"}` (no-op, same graceful-skip philosophy as
the collector). This keeps read and write credentials physically separated.

- **set_docdb_profiler**: connect with the write secret (`retryWrites=False`,
  `tls=True`, CA file), `db.command("profile", level, slowms=slowms)` for the target db
  (level 0/1/2; validate level∈{0,1,2}, slowms ≥ 0). Hardcoded — no generic command.
- **create_docdb_index**: `db[collection].create_index(list(keys.items()),
background=True, name=name)`. Validate keys is a non-empty dict of field→(1|-1);
  require an explicit `name`; `background=True` so it doesn't block the primary. Warn in
  the approval payload that a large-collection build consumes IO.

Mongo-write fail-safe: any pymongo error → `{"status":"error", reason}`; never raise,
never partial-write beyond the single allowlisted command.

## Cedar (`operations_policy.cedar`, manually applied via `agentcore policy create`)

Add the five actions to the **write** permit block (the `when {context.input.approved
== true}` block). They are write-gated exactly like `modify_scaling`.

**`forbid`-unless-`force` for destructive directions (review fix #7):** mirror the
existing `execute_sql` DROP/TRUNCATE `forbid … unless { context.input.force == true }`.
Add a `forbid` for the data-protection/availability degradations so they need an
explicit `force` (which is ALSO hashed into the approval payload, so the DBA approves
the forceful variant specifically):

```cedar
forbid(principal, action == Action::"enable_dynamodb_pitr", resource)
  when  { context.input.enabled == false }      // disabling PITR
  unless { context.input.force == true };
```

(For `modify_dynamodb_capacity`, the `<1` rejection at the tool + the GSI block already
prevent the dangerous shapes; a Cedar `force` for "drop to the 1/1 minimum" is optional
v2 — note it but don't gate v1 on it.)

While here, also add the currently-MISSING `create_snapshot`, `restore_cluster`,
`request_approval`, `query_activity_audit`, `get_runbook` (Explore found them absent →
silent Gateway DENY) — `request_approval`/`query_activity_audit`/`get_runbook` go in the
unconditional read block; `create_snapshot`/`restore_cluster` in the approved-write
block. (Document that the deployer must re-run `agentcore policy create` — Cedar is not
CDK-deployed.)

## Gateway schema + parity

`cdk/tool_definitions.py` `operations_schema()`: add the five `_tool(...)` entries,
each with its args **plus `approved:"boolean"`, `approval_id:"string"`**, `required:
["cluster_id", ...]`. The parity test `test_every_handler_param_is_exposed_in_gateway_schema`
will fail until handler `_impl` signatures and the schema match — that's the safety net.
(operations is excluded from `_READONLY_POLICY`, so no read-parity entry needed.)

## Frontend (Approval Center)

`/approvals` already renders any pending approval generically from `action_type` +
`action_details`. Add human-readable labels + a per-action risk hint for the five new
`action_type`s (mirror the existing approval-card risk guidance). Optionally, a
"이 권장 실행" button on the DynamoDB cost simulator result and the Mongo `profiler_off`
finding that pre-fills a `request_approval` — **defer to a follow-up** to keep this spec
write-tool-focused; v1 just needs the approval cards to render the new actions correctly.

## Testing

- **Unit per tool** (mock `client_for_cluster` / pymongo + `verify_approval`): the
  3-state flow (approval_required / approval_denied / success), idempotency skip, the
  AWS-constraint warnings, billing-mode validation (Provisioned requires RCU/WCU),
  profiler level validation, index keys/name validation, and the never-raise error path.
- **Safety-fix regression tests (one per review finding):**
  - **FAIL-CLOSED (#3):** family=None / capability missing → `unsupported_engine`, NO
    execute (assert the AWS client write method is never called).
  - **`<1` rejection (#4):** rcu/wcu `<1` → denied/error, not silently floored.
  - **GSI block (#5):** a table with a GSI → `unsupported` on capacity change.
  - **TOCTOU (#6):** execute-time re-read shows drift from the approved precondition →
    `approval_denied`, write skipped.
  - **force on destructive (#7):** disable-PITR without `force` → denied; with `force`
    (and a matching hashed approval) → allowed.
- **approval_guard**: a `_project()` test per new action_type asserting the hash binds
  the right fields (changed field → different hash → `approval_denied`). Specifically:
  **compound-index key ORDER (#2)** — `[["a",1],["b",1]]` and `[["b",1],["a",1]]` must
  hash DIFFERENTLY; and the **table target (#1)** is inside the capacity hash. The
  hashed rcu/wcu equals the executed (validated ≥1) value (#4).
- **Cedar parity**: handler tools ↔ Gateway schema params (auto). Manually assert the
  five appear in the operations Cedar write block (extend a test or a doc check).
- **CDK**: `cdk synth dbops-dev-agent` (operations bundling now runs pymongo install) +
  `dbops-dev-data`/`dbops-dev-foundation` unaffected. Confirm the pymongo+CA land in the
  operations asset (md5/ls the synthesized asset, as done for the collector).
- **Frontend**: tsc + build.
- **Live**: DynamoDB end-to-end on the demo table (request → approve via authenticated
  browser → execute → verify the table actually changed, then revert). DocDB Mongo
  writes: **deferred** unless a RW Mongo user/secret is provisioned on the demo cluster
  (same honest limitation as the read collector) — unit + synth verified.

## Safety summary (defense in depth, per write)

1. Cedar `approved==true` at the Gateway. 2. `approval_guard`: DBA-approved + exact
   payload-hash + atomic single-consume + 30-min replay window + fail-closed on missing
   table/action_type. 3. Positive engine-capability gate. 4. Cross-account via scoped
   assume-role. 5. DocDB writes: separate RW Mongo user + scoped secret + hardcoded
   2-command allowlist + in-VPC only. 6. Idempotency pre-checks + AWS-constraint warnings.
2. Audit: DynamoDB/Mongo writes are inherently attributable to the assumed role; the
   approval row records `requested_by` + `action_details` + `consumed_at`.

## Out of scope (v1)

- Per-GSI DynamoDB capacity; auto-scaling policy management.
- Mongo data writes (insert/update/delete), dropIndex, collection drops.
- The "execute this recommendation" one-click buttons (separate UX follow-up).
- Cross-account DocDB Mongo writes needing VPC peering/PrivateLink (local-account first).
