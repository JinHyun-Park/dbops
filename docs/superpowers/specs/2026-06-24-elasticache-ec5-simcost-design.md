# ElastiCache EC-5 — Simulation + Cost / Right-Sizing — Design

**Date:** 2026-06-24
**Status:** approved (final spec of the ElastiCache program; pure-compute, fully unit-testable — the user noted EC-5 needs no live cluster)

## Context

Fifth and final spec of the ElastiCache program. EC-1/2/3/4 give metrics,
findings, live deep-read, and write tools. EC-5 adds **cost intelligence**:
a node-resize cost simulation (what does scaling to node type X / N nodes cost?),
the AWS-priced math behind it, and a right-sizing finding (low CPU → recommend a
smaller node). Mirrors the DynamoDB cost-simulation model
(`simulate_dynamodb_capacity_cost` + `dynamodb_cost.py` + `dynamodb_pricing.py`)
and the Aurora/DocDB `cost_oversized` right-sizing finding.

Pure compute — pricing via the **AWS Price List API** (no hardcoded prices, the
established pattern), so EC-5 is fully unit-testable with no live cluster.

This spec covers **EC-5**. Deferred follow-up (noted, not built here): a Cost-tab
`?view=elasticache` Cost-Explorer actual-spend view — a separate CE-query surface
from the simulation/right-sizing this spec delivers; small, addable later.

## Capability correction (mirror DynamoDB)

EC-1 set `CAPABILITIES["elasticache"]["simulation"] = True`. That is WRONG: the
simulation MCP server's negative gate (`simulation/handler.py:179`,
`not CAPABILITIES[fam].get("simulation", True)`) then DEFAULT-PERMITS the six
Aurora-only tools (upgrade/DDL/param/ACU scaling) for ElastiCache — they would
describe an ElastiCache cluster as RDS and error. DynamoDB avoids this with
`simulation: False` + a dedicated `ddb_cost_simulation: True` positive gate.

EC-5 corrects ElastiCache to the same shape, in all four `engine_family.py`
copies (the frontend `engine.ts` mirror has NO `simulation` field — confirmed —
so it needs no capability change; the frontend simulator branches on
`engineFamily()` directly):

- `"simulation": False` (blocks the 6 Aurora tools for ElastiCache).
- add `"elasticache_cost_simulation": True` (positive-gates the new tool, like
  `ddb_cost_simulation`).

## Architecture

### Component 1 — `mcp-servers/mcp_servers/shared/elasticache_pricing.py` (new)

Mirror `aurora_pricing.py`: `price_per_node_hour(region, engine, node_type) ->
float | None` via `boto3.client("pricing", region_name="us-east-1")`,
`ServiceCode="AmazonElastiCache"`, filters `regionCode`, `instanceType`
(node_type), `cacheEngine` (Redis/Memcached/Valkey — match the Price List
attribute; Valkey may price as Redis), suffix-matched usagetype. Process-level
soft-fail cache `{(region, engine, node_type): price_or_None}`; returns `None` on
any miss (caller marks `partial`). NEVER fabricates a price.

### Component 2 — `mcp-servers/mcp_servers/shared/elasticache_cost.py` (new, pure compute)

`compute_node_resize_cost(engine, region, current_node_type, current_node_count,
new_node_type, new_node_count) -> dict` — resolves both prices via
`elasticache_pricing`, computes:
`current_monthly = current_price × current_node_count × 730`;
`proposed_monthly = new_price × new_node_count × 730`; delta + %. If either price
is `None` → `status="partial"` (the available side reported, the other `None`,
with an honesty note). 730h/month convention. Assumptions captured in the
response (node-hours only — data transfer / snapshot storage / reserved nodes
excluded). No I/O — reused by the MCP tool + the REST handler.

### Component 3 — `simulate_elasticache_node_resize` MCP tool

New `mcp-servers/mcp_servers/simulation/tools/elasticache_scaling_simulation.py`:
`simulate_elasticache_node_resize_impl(cache, cluster_id=None, new_node_type=None,
new_node_count=None) -> dict`. Resolves the cluster via
`client_for_cluster(cluster_id, "elasticache")` + `lookup_cluster` (engine,
region); `describe_replication_groups` → current node_type + node count
(NodeGroups × (1+replicas), or MemberClusters length); calls
`compute_node_resize_cost`. Read-only (no approval — it's a simulation). Returns
current/proposed monthly + delta + unit pricing + data_source + Korean note.

Registered in `simulation/handler.py` with a POSITIVE gate on
`elasticache_cost_simulation` (mirror the `simulate_dynamodb_capacity_cost`
positive gate at handler.py:157-172): `if isinstance(fam,str) and not
CAPABILITIES.get(fam,{}).get("elasticache_cost_simulation", False): return
unsupported_engine`. + `cdk/tool_definitions.py` parity entry.

### Component 4 — REST mirror (`api/simulation/handler.py`)

Add `_simulate_elasticache_node_resize(cluster_id, new_node_type, new_node_count)`
mirroring `_simulate_scaling`: same `compute_node_resize_cost` (shared), reached
via a new route arm. (Confirm how api/simulation routes sub-actions; add the arm +
the `add_routes` path if the simulator frontend calls a dedicated path.)

### Component 5 — `elasticache_cost_oversized` right-sizing finding

Extend `elasticache_findings.py` (EC-2's collector): a 7th rule — read 7-day CPU
(`engine_cpu` preferred, else `cache_cpu`) from `metric_snapshots`; if
`avg < 30%` AND `p95 < 60%` with ≥20 samples → emit `elasticache_cost_oversized`
(severity `info`): "최근 7일 CPU 평균 X% — 한 단계 작은 노드 타입을 검토하세요".
Mirror `cost_check.py`'s `cost_oversized` thresholds. Skip burstable
`cache.t*` node types (like Aurora skips `db.t`). Shares the handler `run_ts`.

### Component 6 — Frontend simulator page

`frontend/src/app/simulator/page.tsx` branches on `engineFamily()` (it already
has a dynamodb branch keyed to `ddb_cost_simulation`). Add an `elasticache`
branch: a node-type/count resize input → calls the new sim endpoint → renders
current/proposed monthly + delta (reusing the dynamodb cost-result card
components). Numbers ≥1000 use `fmtDecimal`/`fmtExact`; Korean copy; `partial`
status shows the honesty note.

## Data Flow

Agent / simulator UI → `simulate_elasticache_node_resize` (positive-gated) →
describe current config + `elasticache_pricing` (Price List API) →
`compute_node_resize_cost` → cost delta. Right-sizing: ETL → `elasticache_findings`
reads 7-day CPU → `elasticache_cost_oversized` → surfaces via the existing
findings pipeline. No live cluster connection (control-plane describe + pricing).

## Error Handling

- Pricing miss → `partial` (never a fabricated dollar figure).
- describe failure → degraded result (costs `None`, shape preserved).
- Positive gate → `unsupported_engine` for non-ElastiCache.
- Finding rule: insufficient CPU samples → no finding; never raises (collector).

## Testing

- **`elasticache_pricing` unit**: mock `boto3 pricing` paginator → returns a price;
  miss → `None`; cache hit avoids a second call.
- **`elasticache_cost` unit**: both prices → correct monthly + delta; one price
  `None` → `partial`; node count math.
- **sim tool unit**: describe + cost → result; positive-gate (non-ElastiCache →
  `unsupported_engine`; ElastiCache → reaches impl); pricing-partial passthrough.
- **right-sizing finding unit**: avg<30/p95<60 + ≥20 samples → `elasticache_cost_oversized`;
  high CPU → none; burstable node skipped; shares snapshot_ts.
- **REST + frontend**: api/simulation arm returns the cost JSON; `npm run build`
  clean with the elasticache simulator branch.
- **5-copy sync** test (engine_family) updated for the capability change; **CDK
  synth** + **tool_definitions parity** green; full unit suite green.

## Security

- Read-only: describe + Price List API reads; no mutation, no new write IAM
  (the sim tool only describes; pricing is a global read). Cross-account describe
  via `client_for_cluster` assume-role.
- No fabricated cost figures (honesty contract) — a missing price is reported as
  `partial`, never guessed.
- IAM: the simulation MCP Lambda needs `pricing:GetProducts` (if not already
  granted for aurora/dynamodb pricing — confirm; reuse) + `elasticache:Describe*`
  (add to the simulation Lambda; the operations Lambda got it in EC-3, the
  simulation Lambda is separate).
