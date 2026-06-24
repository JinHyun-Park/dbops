# ElastiCache EC-4 — Write Tools (approval-gated) — Design

**Date:** 2026-06-24
**Status:** approved (EC-4 scope = the user-chosen "core ops set": node-type scaling + snapshot + reboot + failover-test; all approval-gated, mirroring the existing NoSQL write tools)

## Context

Fourth spec of the ElastiCache program. EC-1/2/3 are read-only (metrics, findings,
live deep-read). EC-4 adds the first ElastiCache **mutations**, each behind the
existing human-in-the-loop approval gate (`approval_guard` FAIL-CLOSED), exactly
like the DynamoDB (`modify_dynamodb_capacity`) and DocDB write tools. These are
ElastiCache **control-plane** APIs (AWS APIs), so cross-account works cleanly via
the existing `client_for_cluster` assume-role path — NO VPC peering needed
(unlike EC-3's native protocol).

User-chosen scope — the **core ops set** (4 tools):

1. **Node-type scaling** — `modify_replication_group(CacheNodeType=…)`.
2. **Snapshot** — `create_snapshot` (Redis/Valkey only — Memcached has no backups).
3. **Reboot** — `reboot_cache_cluster`.
4. **Failover test** — `test_failover` (cluster-mode / multi-replica only).

Deferred (not this spec): parameter-group changes, shard/replica resharding,
engine-version upgrade. Cedar is NOT wired (LOG_ONLY per the closed decision); the
`approval_guard` is the real, active enforcement — same as every other NoSQL write.

## Architecture

Four new tools in the operations MCP server, each mirroring `modify_dynamodb_capacity`:
REQUEST-time `describe` surfaces current state as `approval_required` warnings →
`request_approval` mints a payload-bound DDB row → DBA approves in the Approval
Center → re-call with `approved=True, approval_id` → `verify_approval` consumes the
row (payload-hash, single-use) → EXECUTE the mutation. All via
`client_for_cluster(cluster_id, "elasticache")` (hub-spoke cross-account aware).
Engine-gated on `elasticache_write` (FAIL-CLOSED for non-ElastiCache). Each tool
NEVER raises out → `{"status": "error"|"approval_required"|"approval_denied"|..., ...}`.

### Component 1 — Four write tools (`mcp-servers/mcp_servers/operations/tools/`)

Each `..._impl(cache, cluster_id=None, approved=False, approval_id=None, **kw) -> dict`:

- **`modify_elasticache_node_type.py`** (action_type `modify_elasticache_node_type`):
  - args: `node_type` (e.g. `cache.r7g.large`).
  - REQUEST: `describe_replication_groups` → current `CacheNodeType` as a warning
    ("현재 X → 요청 Y"); reject if node_type missing/equal-to-current.
  - EXECUTE: `modify_replication_group(ReplicationGroupId, CacheNodeType=node_type, ApplyImmediately=True)`.
- **`create_elasticache_snapshot.py`** (action_type `create_elasticache_snapshot`):
  - args: `snapshot_name`.
  - **Redis/Valkey only** — if engine is Memcached → `{"status":"unsupported_engine","reason":"Memcached는 스냅샷 미지원"}`.
  - EXECUTE: `create_snapshot(ReplicationGroupId, SnapshotName=snapshot_name)`.
- **`reboot_elasticache.py`** (action_type `reboot_elasticache`):
  - REQUEST: resolve the member cache-cluster/node ids via describe (warn that a
    reboot briefly disrupts the node).
  - EXECUTE: `reboot_cache_cluster(CacheClusterId=<member>, CacheNodeIdsToReboot=[…])`
    for the resolved member(s). (For a replication group, reboot the primary
    member cache cluster.)
- **`test_elasticache_failover.py`** (action_type `test_elasticache_failover`):
  - args: optional `node_group_id` (default the first/only shard).
  - **Requires a replica / multi-AZ** — reject (`unsupported_engine`/`invalid`) if
    the group has no replica to fail over to.
  - EXECUTE: `test_failover(ReplicationGroupId, NodeGroupId=node_group_id)`.

Resolution of the ElastiCache name + cross-account client: `client_for_cluster(cluster_id, "elasticache")` + `lookup_cluster` for `resource_name`/engine (mirror the dynamodb tool's `table_name_for_cluster` analog — use `lookup_cluster(cluster_id)["resource_name"]`).

### Component 2 — Approval payload projections (`mcp-servers/mcp_servers/shared/approval_guard.py`)

Add four branches to `_project_payload(action_type, payload)` so the approval row
binds to the exact operation (payload-hash single-use):

- `modify_elasticache_node_type` → `{target, node_type}`
- `create_elasticache_snapshot` → `{target, snapshot_name}`
- `reboot_elasticache` → `{target}`
- `test_elasticache_failover` → `{target, node_group_id}`

(`target` = cluster_id, matching the existing projections' convention.)

### Component 3 — Handler registration + engine gate (`mcp-servers/mcp_servers/operations/handler.py`)

- Import the four impls; add four `TOOLS` entries (description marks them
  "ElastiCache only", input_schema with `cluster_id` + op args + `approved`/
  `approval_id`).
- Add all four to `_ENGINE_GATED_TOOLS` with capability `"elasticache_write"`
  (FAIL-CLOSED — a non-ElastiCache cluster, or unresolvable family, is refused).
- Add `_CAP_LABEL["elasticache_write"] = "ElastiCache 클러스터"` (the gate-refusal
  message label).

### Component 4 — `cdk/tool_definitions.py` parity + CDK IAM

- Add the four tools to `cdk/tool_definitions.py` (the handler↔schema parity test).
- `cdk/stacks/agent_stack.py` operations MCP Lambda IAM — add the write actions
  (real mutations, scoped to `*` like the existing dynamodb/docdb write grants):
  `elasticache:ModifyReplicationGroup`, `elasticache:CreateSnapshot`,
  `elasticache:RebootCacheCluster`, `elasticache:TestFailover` (+ the describe
  actions already granted in EC-3 for REQUEST-time reads).

## Data Flow

Agent calls a write tool (no `approved`) → `approval_required` (with the projected
payload + warnings) → `request_approval` (DDB row, payload-hash) → DBA approves on
`/approvals` → agent re-calls with `approved=True, approval_id` → `verify_approval`
consumes the row → `client_for_cluster(...).<mutation>(...)`. Cross-account via
assume-role (control-plane API — no network path needed).

## Error Handling

- Not approved → `approval_required` (echoes the operation + current-state warnings).
- `verify_approval` fail (not found / consumed / cluster/action mismatch / payload
  drift / expired) → `approval_denied` with the guard reason.
- Engine gate → `unsupported_engine` for non-ElastiCache; op-not-applicable
  (snapshot/failover on Memcached or no-replica) → `unsupported_engine`/`invalid`.
- boto3 `ClientError` → `{"status":"error", reason: str(e)[:200]}` — never raises out.
- **TOCTOU:** snapshot/reboot/node-type re-describe at EXECUTE is light (these are
  not capacity-precondition-sensitive like DynamoDB), but node-type still
  re-checks the current type hasn't already changed to the target (idempotency).

## Testing

- **Per-tool unit** (`tests/unit/mcp_servers/operations/test_elasticache_writes.py`),
  mocking `client_for_cluster`/`lookup_cluster`/`verify_approval`:
  - not-approved → `approval_required` (no mutation called).
  - approved + guard-ok → the correct boto3 mutation called with the right args.
  - guard-denied → `approval_denied`, NO mutation called (FAIL-CLOSED).
  - snapshot/failover on Memcached / no-replica → refused, no mutation.
  - node-type missing/equal → rejected.
  - ClientError → `error` (never raises).
- **Approval projection unit** (extend the approval_guard tests): the four
  action_types project to the documented fields; a payload drift changes the hash.
- **Engine-gate unit**: each of the four tools on a non-ElastiCache cluster →
  `unsupported_engine`; on an ElastiCache cluster → reaches the impl.
- **Parity**: `cdk/tool_definitions.py` has all four (the parity test passes).
- **CDK synth** green. Full unit suite green.

## Security

- **Every mutation is approval-gated** (`approval_guard` FAIL-CLOSED, payload-bound,
  single-use) — identical to the DynamoDB/DocDB write model. No write executes
  without a consumed approval row matching the exact operation.
- Engine-gated `elasticache_write` (FAIL-CLOSED): a non-ElastiCache or unresolvable
  cluster is refused before any AWS call.
- IAM grants exactly the four write actions (+ existing describes); scoped to the
  account's ElastiCache. Cross-account via assume-role (`client_for_cluster`),
  least-privilege spoke role.
- No destructive op in scope (no delete-replication-group, no delete-snapshot);
  reboot/failover are disruptive-but-recoverable and require explicit approval.
