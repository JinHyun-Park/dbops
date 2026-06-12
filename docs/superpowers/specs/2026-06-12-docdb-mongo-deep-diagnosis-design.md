# DocumentDB Mongo-protocol deep diagnosis — Design Spec

- **Date**: 2026-06-12
- **Status**: Proposed
- **Decision**: ADR 2026-06-12 (Update) — **Option A**: a thin, read-only pymongo
  collector running in-VPC. NOT the AWS DocDB MCP.

## Goal

Diagnose DocumentDB internals that CloudWatch can't show — live operations
(`currentOp`), server status (`serverStatus`: connections, opcounters, mem, asserts,
cursors), and slow-op profiling (`getProfilingStatus` + `system.profile`). Emit
findings into `cluster_health_findings` (cache-only contract), surfaced in the DocDB
Maintenance Health panel + chat (`get_maintenance_findings`). Read-only.

## Scope reality (discovered during design)

Option A is NOT a small addition to the existing ETL collector, because:

1. **The ETL collector Lambda is NOT in a VPC** (`data_stack.py` ETLCollector has no
   `vpc=`; it only calls public AWS APIs — RDS Data API, CloudWatch, PI, Secrets).
   DocDB listens on 27017 inside a private VPC, unreachable from the ETL Lambda.
2. **pymongo is not packaged** (`etl_collector/requirements.txt` is boto3-only; the
   asset is `from_asset` with no bundling — the Lambda relies on the runtime boto3).

So Option A needs a **new, separate, in-VPC, dependency-bundled, scheduled Lambda**.

## Architecture

### New Lambda: `docdb_mongo_collector` (in-VPC, Docker-bundled)

- `data-pipeline/docdb_mongo_collector/` — its own Lambda with `handler.py` +
  `requirements.txt` (`pymongo>=4`). Bundled via `BundlingOptions` (pip install into
  the asset) since pymongo isn't in the runtime. Ship the DocDB TLS CA
  (`global-bundle.pem`) in the asset for `tls=true, tlsCAFile=...`.
- CDK (`data_stack.py`): define it with `vpc=self.vpc`, a security group allowed to
  reach the DocDB cluster on 27017, `secretsmanager:GetSecretValue` + cache RDS-Data
  access + `CLUSTERS_TABLE` read. Schedule via EventBridge (e.g. every 5 min, same as
  ETL) — or invoke from the ETL flow via async invoke. Standalone schedule is cleaner
  (different VPC/runtime profile).
- **Per-cluster credentials**: read the DocDB connection creds from a Secrets Manager
  secret whose ARN is on the registry row (new optional field `mongo_secret_arn`). The
  secret holds `{username, password, host, port}` for a **least-privilege read-only
  Mongo user**. If a documentdb cluster has no `mongo_secret_arn`, SKIP it (no-op).

### Collector logic (read-only allowlist — NO generic runCommand/eval)

For each documentdb cluster that has `mongo_secret_arn`:

- Connect: `MongoClient(host, tls=True, tlsCAFile=<global-bundle.pem>, retryWrites=False,
serverSelectionTimeoutMS=5000, readPreference="secondaryPreferred")` with the secret creds.
- Run ONLY these read commands (hardcoded allowlist):
  - `serverStatus` → connections.current/available, opcounters, mem.resident, asserts,
    metrics.cursor — emit as metric*snapshots (mongo*\* metric_types) + use for findings.
  - `currentOp(active=true)` → count ops with `secs_running` ≥ threshold (e.g. 10s) →
    finding `docdb_mongo_long_running_ops` (warning/critical by count+duration).
  - `getProfilingStatus` + (if profiling enabled) `system.profile` recent slow ops →
    finding `docdb_mongo_slow_ops` (count + top namespaces over the window). If the
    profiler is OFF, emit an info finding suggesting enabling it (level 1, slowms).
- All findings share the handler `run_ts`; cache-only (no live call from the dashboard).
- Hard fail-safe: any connection/command error → log + skip (never raise; never block).

### Frontend

- Add the `docdb_mongo_*` check_types to `maintenance-health-panel.tsx` CHECK_LABELS +
  a tab (e.g. "Live Ops") for the DocDB family. Render like the other docdb findings.

### Read-only enforcement (defense in depth)

1. A **read-only Mongo user** (the deployer creates it; least privilege).
2. Scoped Secrets Manager secret (only that user's creds).
3. In-VPC + SG scoped to the DocDB cluster only.
4. Hardcoded command allowlist in the collector — no eval/generic runCommand.
5. `retryWrites=False`, read preference secondary — never writes.

## Deployer setup (required to activate; documented in README/runbook)

1. Create a read-only Mongo user on the DocDB cluster.
2. Store its creds in a Secrets Manager secret; put the ARN on the cluster's registry
   row as `mongo_secret_arn`.
3. Ensure the new Lambda's SG can reach the cluster on 27017 (same VPC, or peering).

Absent `mongo_secret_arn`, the collector no-ops — CloudWatch-based DocDB diagnosis
(connections/lag/cursor/cache/CPU + cost) is unaffected.

## Testing

- Unit: collector with a mocked pymongo `MongoClient` — serverStatus/currentOp/profile
  fixtures → asserts the right findings (long-running, slow-ops, profiler-off) + the
  no-secret skip + the never-raise contract.
- CDK synth: the new Lambda + SG + schedule synthesize.
- **Live verification DEFERRED**: the kept demo cluster has no read-only Mongo user /
  secret and the collector isn't reachable without the VPC/SG + creds setup above.
  This is an honest limitation — the feature is unit + synth verified, not live.

## Out of scope

- Mongo-protocol WRITES / remediation (index build, profiler enable as an action) —
  belongs with the broader NoSQL write/remediation + Cedar/approval work.
- Cross-account DocDB (needs VPC peering / PrivateLink) — local-account first.
