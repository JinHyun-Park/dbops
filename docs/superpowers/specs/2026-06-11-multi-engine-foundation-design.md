# Multi-Engine Foundation — Design Spec

- **Date**: 2026-06-11
- **Status**: Approved design (pre-implementation)
- **Spec**: #1 of a 5-spec program (see Program Decomposition)
- **Reviewers**: Codex adversarial review (16 findings) + AWS-docs verification, folded in.

## Context

DBOps is an AI-powered DBA platform currently supporting **only Aurora MySQL/PostgreSQL**.
The platform assumes "cluster + SQL + RDS Data API" almost everywhere: registration filters
`engine.startswith("aurora")`, the ETL collector calls RDS/PI/CloudWatch (`AWS/RDS`) before any
engine branch, the cache `cluster_meta` table is RDS-shaped, and the dashboard renders SQL panels.

Goal: accept **AWS DocumentDB** (cluster/instance-shaped, MongoDB protocol) and **AWS DynamoDB**
(table-shaped, NoSQL, no clusters/instances, IAM auth) as first-class monitored resources, and
group every place resources are enumerated **by DB engine family**.

## Program Decomposition (full parity, delivered as a sequence)

"Full parity" = cover Aurora features that have a meaningful analog on the new engine; replace
SQL-only panels with engine-appropriate equivalents; do NOT fabricate non-existent analogs
(DynamoDB has no slow-query log / EXPLAIN / VACUUM / pg_settings / SQL locks).

1. **Foundation (THIS SPEC)** — engine-family model, registration/discovery for the new engines,
   ETL dispatch + new CloudWatch collectors, a neutral resource-meta model, an **enforced
   capability-gating layer** (frontend panels + backend endpoints + collectors), engine-family
   grouping across all enumeration points, and engine-appropriate dashboard shells.
2. **DocumentDB diagnosis** — deeper findings (connection saturation, replica lag, cursor/opcounter
   pressure, cache-hit), Maintenance Health extension, optional profiler-based slow-op.
3. **DynamoDB diagnosis** — capacity (consumed vs provisioned / on-demand), throttle, hot-partition,
   GSI health, cost (capacity-mode), dedicated findings + dashboard depth.
4. **MCP tools + AI** — per-engine MCP diagnosis/write tools, agent prompt/cheatsheet, approval reuse.
5. **Simulation / Cost** — extend simulators and cost analysis to the new engines.

Each spec ships and is verified independently.

## Decisions (locked with user)

- **Depth**: full parity, sequenced into the 5 specs above.
- **DynamoDB unit**: **Table = resource**, grouped by account/region. (Tables are the unit of DBA
  attention — capacity/throttle/GSI are per-table.)
- **Generalization strategy**: **keep names** — `cluster_id` stays the registry PK and `/api/clusters`
  routes are unchanged (no data migration, no broken deep links). `cluster_id` is semantically a
  "resource id." A thin engine-family + capability layer is added on top.
- **Cross-account ETL**: **deferred**. Foundation collects resources in the deployment account only
  (same limitation Aurora has today — the ETL is not assume-role-aware). Cross-account ETL is a
  separate, pre-existing gap tracked as its own small spec. Documented as a known limitation.
- **Spec structure**: Foundation is a **single spec** (data model + registration + ETL + gating +
  grouping/shell are tightly coupled).

## Architecture

### 1. Engine-family model (the spine)

- Derive `engine_family` from `engine`:
  - `relational` ← `aurora-postgresql`, `aurora-mysql` (and bare `postgres`/`mysql`)
  - `documentdb` ← `docdb`
  - `dynamodb` ← `dynamodb`
- New `engine` values: DocumentDB = `docdb`, DynamoDB = `dynamodb`.
- **`cluster_id` scheme** (resolves the validation blocker — see Risks/Finding #3):
  - Relational/DocumentDB: `cluster_id` = the real cluster identifier (already matches the existing
    validator `^[a-zA-Z0-9-]{1,63}$`).
  - DynamoDB: `cluster_id` = **opaque regex-safe slug** `ddb-<12-hex of sha256(account:region:table)>`
    (deterministic, stable, ≤63 chars, `[a-z0-9-]` only). The human table name lives in `resource_name`.
  - Rationale: DynamoDB table names allow `_`/`.` and up to 255 chars, which the existing
    `CLUSTER_ID_RE` (used by `api/dashboard/handler.py`, `api/alerts/handler.py`) would reject. A slug
    keeps every existing path/validator working untouched.
- New registry fields (DynamoDB clusters table item): `resource_name` (display name = table name /
  cluster id), `resource_type` (`aurora`|`docdb`|`dynamodb-table`), `engine_family`. Existing fields
  (`account_id`, `region`, `engine`, `engine_version`) retained.
- Shared helpers:
  - Backend: `mcp-servers/.../shared/engine_family.py` (or a tiny module reused by `api/` and
    `data-pipeline/`) — `engine_family(engine) -> str`, plus a `CAPABILITIES` map (see §5).
  - Frontend: extend `frontend/src/lib/engine.ts` — `engineFamily()`, family labels/badges/colors,
    per-family display noun ("클러스터" / "테이블"). Keep `engineKind()` for relational sub-typing.

### 2. Data model — neutral resource meta

- The cache `cluster_meta` table is RDS-shaped (`instance_class`, `endpoint`, `storage_size_gb`,
  `max_connections`, `serverlessv2_*`...). DynamoDB/DocDB meta does not fit those columns.
- **Schema v16**: add `resource_details JSONB` column to `cluster_meta`. Relational rows keep using
  the typed columns; non-relational rows store engine-specific meta in `resource_details`:
  - DynamoDB: `billing_mode` (PROVISIONED|PAY_PER_REQUEST), `item_count`, `table_size_bytes`,
    `gsi: [{name, ...}]`, `lsi`, `streams_enabled`, `ttl_enabled`, `pitr_enabled`, `table_status`.
  - DocumentDB: `instances: [{id, class, role}]`, `instance_class` (writer), engine version, etc.
    (DocDB largely fits existing columns; `resource_details` holds the extras.)
- `metric_snapshots` (generic `cluster_id, ts, metric_type, value, dimensions`) is reused as-is for
  all engines — no schema change for metrics. New `metric_type` strings per engine (see §4).

### 3. Registration & discovery (per-family)

- **Discovery** (`api/clusters/handler.py`, currently unfiltered Aurora scan + `startswith("aurora")`):
  generalize to enumerate per family with the right client and tag each with `engine`/`engine_family`:
  - Aurora: `rds.describe_db_clusters` (current).
  - DocumentDB: `docdb.describe_db_clusters` (purpose-built API; deliberately chosen over the RDS
    scan). Requires `docdb:DescribeDBClusters`.
  - DynamoDB: `dynamodb.list_tables` + `dynamodb.describe_table`.
- **Registration / test-connection** (`_handle_register`, `_test_connection`, currently RDS-only):
  split validation by family:
  - Aurora: current path (describe-db-clusters + master secret for Data API).
  - DocumentDB: `docdb.describe_db_clusters`; **no Data API secret required** for Foundation
    (CloudWatch only). Mongo query credentials are spec #2+.
  - DynamoDB: `dynamodb.describe_table`; no ARN/secret needed.
  - Add a per-family setup-status field (e.g. `requires_secret_for_foundation=false` for docdb/dynamodb)
    so the UI does not render `secret_source=missing` as an error.
- Bulk register delegates to the same family-aware path.

### 4. Metrics collection (Foundation = CloudWatch + describe only)

**ETL dispatch fix (blocker #2)**: the per-resource loop currently calls `collect_cluster_meta`
(RDS `describe_db_clusters`), `describe_db_instances`/PI, and `collect_cw_metrics` (`AWS/RDS`,
`DBClusterIdentifier`) **before** any engine branch. Restructure to **dispatch on `engine_family`
first**, then construct only the collectors valid for that family:

- `relational`: existing path unchanged (meta + PI + AWS/RDS CW + SQL collectors + param_fitness +
  capacity_forecast + cost).
- `documentdb`: DocDB meta (`docdb.describe_db_clusters` → `cluster_meta` + `resource_details`) +
  **new `docdb_cw_collector`** (namespace `AWS/DocDB`). No PI, no SQL collectors.
- `dynamodb`: DynamoDB meta (`describe_table` → `resource_details`) + **new `dynamodb_cw_collector`**
  (namespace `AWS/DynamoDB`). No RDS/PI/SQL.

**DocDB CloudWatch (verified, namespace `AWS/DocDB`)** — Foundation metric set:
`CPUUtilization`, `DatabaseConnections`, `DatabaseCursors`, `DatabaseCursorsTimedOut`,
`DBClusterReplicaLagMaximum`, `BufferCacheHitRatio`, `FreeableMemory`, `VolumeBytesUsed`,
`ReadLatency`, `WriteLatency`, `DiskQueueDepth`, `EngineUptime`, opcounters
(`OpcountersQuery/Insert/Update/Delete/Getmore/Command`).

- Dimensions: cluster-scoped metrics use `DBClusterIdentifier`; instance-scoped metrics
  (CPU, connections, cache-hit, FreeableMemory) use `DBInstanceIdentifier` for the writer instance
  (enumerate instances from `describe_db_clusters`). Document which metric is cluster vs instance.

**DynamoDB CloudWatch (verified, namespace `AWS/DynamoDB`, dimension `TableName`)** — capacity-mode aware:

- Always: `ConsumedReadCapacityUnits`, `ConsumedWriteCapacityUnits` (stat **`Sum`** for throughput math),
  `ReadThrottleEvents`, `WriteThrottleEvents`, `ThrottledRequests` (`Sum`), `ReturnedItemCount`,
  `SuccessfulRequestLatency`.
- `SuccessfulRequestLatency` and `ReturnedItemCount` **require an `Operation` dimension**
  (GetItem/Query/Scan/PutItem/...). Foundation collects a core operation set; no table-wide latency exists.
- Provisioned-mode tables only: `ProvisionedReadCapacityUnits`, `ProvisionedWriteCapacityUnits`
  (gated by `DescribeTable.BillingModeSummary`). On-demand tables skip Provisioned* (use consumed +
  `OnDemandMax*RequestUnits` where relevant).
- GSI metrics require `GlobalSecondaryIndexName`. Foundation: table-level first; GSI-level is a
  bounded add (enumerate GSIs from `describe_table`).
- Add a `list_metrics`/mock contract test per engine so exact metric/dimension/stat tuples are pinned.

### 5. Capability map + enforced gating (central deliverable)

A declarative `engine_family -> capabilities` map, enforced on **both** ends (this is the part the
review showed cannot be "out of scope" — unguarded code renders empty/misleading/garbage panels and
emits Aurora findings for non-relational resources):

```
relational: { queryLab, explain, vacuum, settings, extensions, schema, locks, waitEvents,
               slowQueries, indexRecs, topology, backups, sqlCapacityForecast, costRightsizing,
               maintenanceHealth(pg/mysql), paramFitness }
documentdb: { connections, cursors, replicaLag, cacheHit, opcounters, instances, backups(rds),
               topology(rds) }   // SQL/PI/vacuum/extensions: false
dynamodb:   { capacity(consumed/provisioned/on-demand), throttles, latencyByOp, itemCount,
               tableSize, gsiHealth, costByMode }   // clusters/instances/SQL/PI/backups: false
```

- **Frontend gating**: `dashboard/page.tsx` renders the panel set for the resource's `engine_family`
  from the map (relational unchanged; docdb/dynamodb get their sets). No SQL/RDS panel renders for a
  resource whose family lacks the capability.
- **Backend gating**: endpoints that hit RDS live APIs must check family and no-op for non-relational
  rather than calling `describe_db_clusters` on a DynamoDB id:
  - `api/dashboard/handler.py`: topology, backups, capacity-forecast endpoints → return
    `not_applicable` / empty for non-relational families.
  - `_health_findings` → only returns findings the family actually produces (gated by check_type).
- **Collector gating**: `cost_check`, `capacity_forecast`, and any findings collector run **only for
  families they support**. In Foundation, cost/capacity/param collectors are relational-only;
  non-relational resources collect metrics + meta but emit no findings yet (specs #2/#3 add them).
  This prevents the cost collector from emitting account-level Savings-Plan findings against a
  DynamoDB resource.
- **Agent/MCP guard**: add an engine-family capability check in the target-resolution path
  (`execute_sql`/`cache_client.execute_on_target`) so the agent returns a clear
  "this resource type isn't supported in chat yet (phase 1)" instead of attempting SQL and failing
  silently. System prompt notes the supported families.

### 6. Frontend — grouping + family-aware UI

- `lib/engine.ts`: add `engineFamily()` + family labels/badges/colors + display noun.
- Shared `groupByEngineFamily(resources)` util applied to every enumeration point:
  Fleet (`fleet/page.tsx` — currently groups by raw engine string; refine to family),
  dashboard chip-strip, `cluster-dropdown.tsx`, `command-palette.tsx`, `clusters/page.tsx`,
  `compare/page.tsx`. Each shows family headers + counts; dropdown/search display `resource_name`
  (not the opaque slug) with the family badge.
- **Compare**: restrict candidate B to the same family as A; block cross-family saved URLs.
- **Clusters registration form**: add DocumentDB + DynamoDB engine options + family-appropriate fields.
- **Dashboard shell per family**: relational unchanged; documentdb = cluster/instance info,
  connections, replica lag, cache hit, cursors, opcounters, backup; dynamodb = table info
  (billing mode, item count, size, GSIs), capacity (consumed vs provisioned/on-demand), throttles,
  latency-by-op, cost-by-mode. Driven by the capability map (§5).

## Cross-account (deferred — documented limitation)

ETL `get_client(service, region)` is local-account only (no assume-role), unlike `api/` and
`mcp-servers/.../shared/cluster_targets.py`. Foundation therefore collects metrics for resources in
the **deployment account** only — identical to Aurora's current behavior. Cross-account ETL
(assume-role keyed by `(service, region, role_arn)`, reusing the `cluster_targets` pattern) is a
separate, pre-existing gap tracked as its own spec. Dev verification uses local-account resources.

## CDK / IAM

- ETL Lambda role (and, later, spoke role) gains `dynamodb:ListTables`, `dynamodb:DescribeTable`,
  `docdb:DescribeDBClusters`; `cloudwatch:GetMetricStatistics`/`ListMetrics` already present.
- All via CDK (`cdk/stacks/data_stack.py` ETL role, discovery role in `api`). No console changes.

## Testing

- **Backend unit**: `engine_family` derivation; DynamoDB slug id generation/uniqueness; discovery
  generalization (mock rds/docdb/dynamodb); per-family registration validation; `docdb_cw_collector`
  and `dynamodb_cw_collector` (mock CloudWatch incl. capacity-mode + Operation/GSI dims + `Sum`);
  capability gating (cost/capacity/findings skip non-relational; backend endpoints return
  `not_applicable`).
- **Frontend**: `engineFamily()`/labels, `groupByEngineFamily` util, compare same-family guard, tsc.
- **Live (dev, local account)**: register a real **DynamoDB table** → verify meta in
  `resource_details`, metrics in `metric_snapshots`, grouped UI, DynamoDB dashboard shell, and that
  Aurora-only panels/endpoints/findings do NOT render/run for it. DocumentDB: if no DocDB cluster
  exists in dev, verify via mock + code review; otherwise end-to-end.

## Out of scope (later specs)

- DocDB/DynamoDB deep findings & Maintenance Health (#2/#3); MCP diagnosis/write tools + agent
  cheatsheet (#4); simulation/cost depth (#5); writes + approval flows for new engines (#4);
  cross-account ETL assume-role (separate spec).

## Risks & mitigations (from adversarial review)

| #        | Risk                                                                                  | Mitigation                                                                        |
| -------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| 1        | ETL not cross-account                                                                 | Deferred + documented; Foundation = deployment account only.                      |
| 2        | RDS/PI/CW called before engine branch → DynamoDB errors every cycle                   | Dispatch by `engine_family` before constructing collectors.                       |
| 3        | Composite `cluster_id` rejected by `^[a-zA-Z0-9-]{1,63}$` validators                  | DynamoDB uses regex-safe slug; `resource_name` holds table name.                  |
| 4        | Registration/test-connection Aurora-only                                              | Per-family validation; no secret for docdb/dynamodb Foundation.                   |
| 9        | `cluster_meta` RDS-shaped                                                             | `resource_details JSONB` column (schema v16).                                     |
| 10/11/14 | Panels/endpoints/collectors render/run unconditionally → empty/garbage/false findings | Enforced capability gating on frontend panels, backend endpoints, and collectors. |
| 15       | Agent attempts SQL on unknown engine                                                  | Capability guard in target resolution + system-prompt note.                       |

## AWS facts (verified via AWS docs)

- DocumentDB CloudWatch namespace = **`AWS/DocDB`**; dimensions `DBClusterIdentifier`,
  `DBClusterIdentifier,Role`, `DBInstanceIdentifier`. `BufferCacheHitRatio` exists (System metrics);
  cursor-timeout metric is `DatabaseCursorsTimedOut`.
- DynamoDB CloudWatch namespace = **`AWS/DynamoDB`**; dimensions include `TableName`,
  `GlobalSecondaryIndexName`, `Operation`. `Consumed*` use `Sum`; `Provisioned*` exist only for
  provisioned-mode tables; `SuccessfulRequestLatency` requires `Operation`.
