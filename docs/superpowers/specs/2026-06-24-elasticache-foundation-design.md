# ElastiCache as a DBOps Engine Family — EC-1 Foundation — Design

**Date:** 2026-06-24
**Status:** approved (user approved the decomposition + EC-1 scope; design decisions per the "proceed" directive)

## Context: the ElastiCache program

ElastiCache (Redis OSS / Valkey / Memcached) is being added as a first-class
DBOps engine family, alongside the existing `relational` (Aurora MySQL/PG),
`documentdb`, and `dynamodb` families. The codebase is already factored for
multi-engine: a canonical `engine_family()` + `CAPABILITIES` map (4 verbatim
copies + a frontend mirror), engine-branched registration/ETL/MCP-gating/
approval projections, and per-family dashboard panels. ElastiCache fills in the
same branch points — no new infrastructure abstraction.

Per the approved decomposition, the full vertical ships as a 5-spec program
(mirroring the DocumentDB/DynamoDB program):

- **EC-1 Foundation (THIS spec)** — engine-family extension, discover/register,
  cluster metadata, CloudWatch ETL collector, basic dashboard. Read-only,
  CloudWatch-only. Makes ElastiCache registered, collected, and visible.
- **EC-2** — read diagnosis / findings (eviction, hit-rate, memory pressure,
  replication lag, connection saturation) + read MCP tools + incident RCA signals.
- **EC-3** — live Redis/Memcached deep-read (in-VPC: INFO/SLOWLOG/CLIENT LIST/
  MEMORY STATS + Memcached `stats`), AUTH-token secret, redis client bundle.
- **EC-4** — approval-gated write tools (scaling, parameter groups, failover
  test, reboot, snapshot, engine upgrade) — FAIL-CLOSED + Cedar `elasticache_write`.
- **EC-5** — scaling/parameter/upgrade simulation + right-sizing + Cost tab.

This spec covers **EC-1 only**. Engines in scope across the program:
**Redis OSS + Valkey + Memcached** (node-based clusters + Redis/Valkey
replication groups, incl. cluster-mode). Serverless ElastiCache is out of scope
(deferred). The CloudWatch namespace for all is `AWS/ElastiCache`.

## Problem

DBOps cannot see or monitor ElastiCache. A DBA running a Redis/Valkey/Memcached
fleet has no registration, no metrics, no dashboard for it inside DBOps.

## Goal (EC-1)

Register ElastiCache clusters, collect their CloudWatch metrics into the Aurora
PG cache on the existing ETL cadence, and render an ElastiCache dashboard — using
the SAME abstractions as DynamoDB/DocumentDB so EC-2..EC-5 layer on cleanly.

Non-goals (EC-1): findings/diagnosis (EC-2), live protocol deep-read (EC-3),
any write/mutation (EC-4), simulation/cost (EC-5), Serverless ElastiCache.

## Architecture

A new engine family `elasticache` added to the canonical model and its ~6 branch
points. No new infra construct; the ETL Lambda, dashboard API, and frontend all
already branch by family.

### Component 1 — Engine-family model (5 synchronized copies)

Add to all four `engine_family.py` copies (`api/clusters/`, `api/dashboard/`,
`data-pipeline/etl_collector/collectors/`, `mcp-servers/mcp_servers/shared/`)
AND the frontend mirror `frontend/src/lib/engine.ts`:

- Constant `ELASTICACHE = "elasticache"`.
- `engine_family()` detection BEFORE the relational fallback:
  `if "redis" in e or "valkey" in e or "memcached" in e or "elasticache" in e: return ELASTICACHE`.
  (Order: docdb/dynamodb/elasticache explicit, relational fallback last.)
- `CAPABILITIES[ELASTICACHE]`:
  ```python
  ELASTICACHE: {
      "sql": False, "rds_meta": False, "perf_insights": False,
      "simulation": True,            # EC-5 turns this on in the UI/sim path
      "elasticache_write": True,     # EC-4 write-tool capability gate
      "live_read": True,             # EC-3 live deep-read capability gate
      "cw_namespace": "AWS/ElastiCache",
      "findings": {"elasticache"},   # EC-2 emits these; EC-1 emits none
  },
  ```
  EC-1 wires the family + metrics only; `findings`/`simulation`/`elasticache_write`/
  `live_read` flags are declared now (so later specs flip behavior without
  re-touching the map) but no EC-1 code path acts on them beyond metrics.
- Frontend `engine.ts`: extend the `EngineFamily` union with `"elasticache"`,
  the `engineFamily()` string match, `FAMILY_META` (label "ElastiCache", color),
  and `FAMILY_PANELS.elasticache` (see Component 4).

### Component 2 — Discovery + registration (`api/clusters/handler.py`)

- **Registration dispatch**: extend `_handle_register` —
  `if fam == "elasticache": return _register_elasticache(table, body)`.
- **`_register_elasticache`**: validate the resource exists via the ElastiCache
  control API:
  - Redis/Valkey replication group (cluster-mode or replica set): `describe_replication_groups(ReplicationGroupId=name)`.
  - Standalone cache cluster / Memcached: `describe_cache_clusters(CacheClusterId=name, ShowCacheNodeInfo=True)`.
  - Try replication group first, fall back to cache cluster (a name can be either).
  - Registry PK: the real ElastiCache name (matches the existing
    `^[a-zA-Z0-9-]{1,63}$` validator — ElastiCache names are
    `^[a-z][a-z0-9-]{0,49}$`, so NO slug needed, unlike DynamoDB).
  - Store: `engine` = the reported engine (`"redis"`/`"valkey"`/`"memcached"`),
    `engine_family="elasticache"`, `resource_type` =
    `elasticache-redis|elasticache-valkey|elasticache-memcached`,
    `requires_secret_for_foundation=False` (EC-1 needs no secret; EC-3 adds the
    AUTH-token secret).
  - `resource_details` (JSONB in cluster_meta): `engine`, `engine_version`,
    `node_type`, `num_node_groups` (shards), `replicas_per_node_group`,
    `cluster_mode` (enabled/disabled), `num_cache_nodes` (Memcached),
    `auth_enabled` (Redis AUTH), `tls_enabled` (TransitEncryption), `status`.
- **Discovery**: add ElastiCache enumeration to the discover path —
  `describe_replication_groups` + `describe_cache_clusters` (paginated),
  returning candidate names + engine for the discover UI (same shape as the
  Aurora/DynamoDB discover entries).
- IAM: the clusters Lambda gets `elasticache:DescribeReplicationGroups`,
  `elasticache:DescribeCacheClusters` (+ for cross-account, via the existing
  assumed-role session helper). Read-only describe.

### Component 3 — ETL CloudWatch collector

- New `data-pipeline/etl_collector/collectors/elasticache_cw_collector.py`,
  mirroring `dynamodb_cw_collector.py` / `docdb_cw_collector.py`:
  - Namespace `AWS/ElastiCache`. Primary dimension `CacheClusterId`
    (for a replication group, iterate its member cache clusters / node IDs; the
    collector resolves member node ids from `resource_details` or a describe).
  - Metrics (Redis/Valkey): `CPUUtilization`, `EngineCPUUtilization`,
    `DatabaseMemoryUsagePercentage`, `BytesUsedForCache`, `CacheHits`,
    `CacheMisses` (Sum → derive hit-rate downstream), `CurrConnections`,
    `NewConnections`, `Evictions`, `Reclaimed`, `ReplicationLag`, `SwapUsage`,
    `FreeableMemory`, `CurrItems`, `NetworkBytesIn`, `NetworkBytesOut`.
  - Metrics (Memcached): the Memcached subset — `CPUUtilization`,
    `FreeableMemory`, `SwapUsage`, `CurrConnections`, `NewConnections`,
    `Evictions`, `Reclaimed`, `CurrItems`, `BytesUsedForCacheItems`,
    `NetworkBytesIn/Out`, `GetHits`, `GetMisses` (no replication/persistence).
    The collector branches on `resource_details.engine`.
  - Store into `metric_snapshots` with `metric_type` strings (e.g.
    `"cache_cpu"`, `"engine_cpu"`, `"memory_usage_pct"`, `"bytes_used"`,
    `"cache_hits"`, `"cache_misses"`, `"curr_connections"`, `"evictions"`,
    `"replication_lag"`, `"swap_usage"`, `"freeable_memory"`, `"curr_items"`,
    `"net_in"`, `"net_out"`). Same row shape (cluster_id, metric_type, value,
    snapshot_time, dimensions) the existing readers consume.
  - **metric_snapshots dimensioned-rows caveat** (project memory): if any
    metric is stored per-node with a `dimensions` JSON, every reader that
    aggregates that `metric_type` must exclude dimensioned rows
    (`NOT jsonb_exists(dimensions,'node')`) to avoid mixed aggregation. EC-1
    stores cluster-level (node-aggregated) rows by default; if node-level is
    added, it carries a `dimensions` marker and readers filter on it.
- `etl_collector/handler.py` `_collect_one`: add
  `elif family == "elasticache": collect_elasticache_metrics(...)` (no findings
  collector in EC-1). The collector shares the handler `run_ts` (project memory:
  finding/metric collectors must share the handler timestamp).

### Component 4 — Dashboard

- `frontend/src/lib/engine.ts` `FAMILY_PANELS.elasticache`:
  `{"overview", "memory", "hitRate", "connections", "evictions", "throughput", "replicationLag"}`
  (replicationLag hidden for Memcached / cluster-mode-disabled single-node via
  the panel's own data-presence guard).
- New `frontend/src/components/dashboard/elasticache-overview-panel.tsx`
  (mirror `dynamodb-overview-panel.tsx`): memory usage %, hit rate (derived from
  hits/misses), evictions, current connections, CPU/engine-CPU, replication lag,
  network throughput — reading the cached metric series.
- `frontend/src/app/dashboard/page.tsx`: add `{fam === "elasticache" && (
<ElasticacheOverviewPanel ... /> )}` branch.
- `api/dashboard/` backend: ensure the metrics endpoints return the ElastiCache
  `metric_type` series (they are metric-type-agnostic readers; verify the
  elasticache types flow through and add an overview aggregation endpoint if the
  dynamodb/docdb pattern has a dedicated one).

## Data Flow

CloudWatch `AWS/ElastiCache` → ETL Lambda (`collect_elasticache_metrics`, family
branch) → Aurora PG `metric_snapshots` → REST `/api/dashboard/...` → frontend
ElastiCache panels. The ONLY live AWS call is `describe_*` at registration/
discovery. No SQL, no Data API, no cache-protocol connection in EC-1.

## Error Handling

- Registration: `describe_*` failure (not found / no permission) → reject with a
  clear error (mirror Aurora/DynamoDB register failures). Try-replication-group-
  then-cache-cluster handles the ambiguous-name case.
- Collector: missing/empty CloudWatch series → store nothing for that metric
  (empty series tolerated downstream); never raise out of `_collect_one` for one
  resource (isolate per-resource like the existing collectors).
- Dashboard: absent metrics → the panel shows an empty/`—` state (existing
  panel-shell empty handling).

## Testing

- **engine_family** unit (all copies behave identically): `redis`/`valkey`/
  `memcached`/`elasticache-redis` → `elasticache`; relational/docdb/dynamodb
  unchanged; `CAPABILITIES[elasticache]` shape.
- **`_register_elasticache`** unit (mock `elasticache` client): replication-group
  path, cache-cluster fallback path, Memcached path, not-found → reject,
  resource_details population, PK = real name.
- **collector** unit (mock CloudWatch `get_metric_data`): Redis metric set →
  metric_snapshots rows with correct `metric_type`; Memcached branch → Memcached
  subset; empty series tolerated; shares `run_ts`.
- **ETL dispatch** unit: `family=="elasticache"` routes to the collector.
- **CDK**: `tests/cdk/test_synth.py` green (clusters Lambda gains elasticache
  describe IAM; ETL Lambda unchanged structurally).
- **Frontend**: `npm run build` clean; `engineFamily()` mirror test if present.
- **OpenAPI**: if any new route is added, regenerate `frontend/public/openapi.json`
  (`python tools/openapi_gen.py`) — route-table parity test. (EC-1 likely adds
  no new route; registration reuses `/api/clusters`.)

## Security

- EC-1 is read-only: only `elasticache:Describe*` (registration/discovery) +
  CloudWatch reads (ETL). No mutation, no secret, no protocol connection.
- IAM scoped to describe/CloudWatch actions; cross-account via the existing
  assumed-role session helper (same as Aurora/DynamoDB cross-account reads).
- Write/auth/connectivity surface is intentionally deferred to EC-3/EC-4.
