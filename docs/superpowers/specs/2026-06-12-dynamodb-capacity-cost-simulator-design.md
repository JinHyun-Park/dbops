# DynamoDB capacity-mode cost simulator — Design Spec

- **Date**: 2026-06-12
- **Status**: Proposed
- **Program**: Multi-engine #P3.6 Group C (final remaining: this + NoSQL write/remediation)
- **Decision basis**: ADR 2026-06-12 (Option A — first-party bounded reads, cache-first,
  honest pricing) + Explore research of the existing simulation/pricing/cost surfaces.

## Goal

Let a DBA see the **$ what-if of switching a DynamoDB table's billing mode**
(Provisioned ↔ On-Demand), computed from the table's **actual consumed capacity**
(already in the cache) priced with the **real AWS Price List API** for the table's
region. A wrong number is worse than none — if pricing can't be resolved, show
`source: fallback` / no-data, never a fabricated figure (mirrors `aurora_pricing`).

This is the DynamoDB analogue of the Aurora scaling-cost simulator. It is
**read-only** (no Cedar/approval): it reads cache + calls Pricing, computes, returns.

## Why now / what exists

- #3 already emits `ddb_capacity_overprovisioned` / `_underprovisioned` findings, but
  those are qualitative ("you're over-provisioned"), not a **dollar** comparison of the
  two modes. This closes that gap.
- `aurora_pricing.py` is the proven pattern: Price List API, `regionCode` FILTER (not the
  client region), process-level `_CACHE`, soft-fail → `None`. We mirror it for DynamoDB.
- `pricing:GetProducts` IAM is **already** on the simulation MCP Lambda and the simulation
  REST Lambda (CDK `agent_stack.py:166, 444`) — **no CDK change needed**.
- The simulator page already family-gates (`fam !== "relational"` → EmptyState). We open a
  `fam === "dynamodb"` branch. `FAMILY_PANELS["dynamodb"]` already lists `"cost"`.

## Architecture

### New: `dynamodb_pricing.py` (2 byte-identical copies)

Paths (mirror the two `aurora_pricing.py` copies):

- `mcp-servers/mcp_servers/shared/dynamodb_pricing.py`
- `api/simulation/dynamodb_pricing.py`

`ServiceCode="AmazonDynamoDB"`, client in `us-east-1`, `regionCode` as a TERM_MATCH
filter. Process-level `_CACHE` keyed `(kind, region)`. Every lookup fails soft → `None`.
Reuse the `_on_demand_usd(product)` extractor verbatim.

Four prices, all `$ per unit`, matched by `usagetype` **suffix** (region prefix varies,
so match suffix to stay region-agnostic — same technique as the ACU lookup):

| function                        | billing     | usagetype suffix to match | unit                          |
| ------------------------------- | ----------- | ------------------------- | ----------------------------- |
| `price_per_rcu_hour(region)`    | Provisioned | `ReadCapacityUnit-Hrs`    | $/RCU-hour                    |
| `price_per_wcu_hour(region)`    | Provisioned | `WriteCapacityUnit-Hrs`   | $/WCU-hour                    |
| `price_per_million_rru(region)` | On-Demand   | `ReadRequestUnits`        | $/million read request units  |
| `price_per_million_wru(region)` | On-Demand   | `WriteRequestUnits`       | $/million write request units |

> Implementer note: confirm the exact `usagetype`/`group` strings against a live
> `get_products(ServiceCode="AmazonDynamoDB", Filters=[regionCode])` sample during
> implementation (suffixes above are from AWS docs; the API is source of truth). The
> on-demand price is published per-request; AWS lists per **million** request units in
> some regions and per-request in others — normalize to $/request-unit internally and
> label clearly. If a suffix doesn't match, return `None` (→ fallback), never guess.

Bound pagination with `_MAX_PAGES` like `aurora_pricing` (DynamoDB region SKU count is small).

### New MCP tool: `simulate_dynamodb_capacity_cost`

`mcp-servers/mcp_servers/simulation/handler.py` — add to `TOOLS` + an `_impl`.

- **Gate**: add a new positive capability key `ddb_cost_simulation: True` to the
  `"dynamodb"` block in **all 4** `engine_family.py` copies (leave `simulation: False`
  untouched so the other simulation tools still cleanly refuse DynamoDB). The handler's
  generic `simulation` guard stays; this tool checks its own key:
  ```python
  if not CAPABILITIES.get(fam, {}).get("ddb_cost_simulation", False):
      return {"status": "unsupported_engine", "engine_family": fam,
              "hint": "용량/비용 비교는 DynamoDB 전용 시뮬레이터를 사용하세요"}
  ```
  Mirror the TS `engine.ts` capability/panel mirror.
- **Input**: `cluster_id`; optional `headroom` (default 0.70 target utilization for the
  provisioned sizing), optional `window_hours` (default = whatever's in cache, cap 168).
- **Reads** (cache, RDS Data API):
  - `cluster_meta.resource_details->>'billing_mode'` (+ region from `cluster_meta`).
  - `consumed_rcu` / `consumed_wcu` 1-min Sum series, `dimensions = '{}'` only
    (exclude per-GSI rows — same dimension-mixing trap fixed on the dashboard).
  - If PROVISIONED: latest `provisioned_rcu` / `provisioned_wcu` for the _current_ cost.
- **Math** (document every assumption in the response):
  - **On-Demand monthly** = `(total_RRU/1e6 × $/Mrru) + (total_WRU/1e6 × $/Mwru)`, where
    `total_RRU` = sum of `consumed_rcu` over the window scaled to 730h (1 consumed RCU ≈
    1 RRU for ≤4KB strong reads — state the approximation). Same for writes.
  - **Provisioned monthly** = `rcu_sized × $/RCU-hr × 730 + wcu_sized × $/WCU-hr × 730`,
    where `rcu_sized = ceil( p99(consumed_rcu/60) / headroom )` per-second capacity
    (p99 not max, to avoid pricing a one-off spike; headroom models auto-scaling target).
  - **current_monthly** = the table's actual mode cost (provisioned: from the real
    provisioned units; on-demand: = the On-Demand figure).
  - **recommendation** = cheaper of the two, with the $ delta and % — but only when BOTH
    prices resolved. If either price is `None` → `status: "partial"`, `source: "fallback"`,
    omit the missing side, never fabricate.
- **Output** (JSON): `{status, billing_mode, region, window_hours, datapoints,
on_demand_monthly_usd, provisioned_monthly_usd, current_monthly_usd, recommended_mode,
monthly_savings_usd, savings_pct, sizing:{rcu_per_sec, wcu_per_sec, basis:"p99",
headroom}, pricing_source:"aws_pricing_api"|"fallback", assumptions:[...]}`.
- **No-data**: < `MIN_DATAPOINTS` (e.g. 20) consumed datapoints → `status:"no_data"`,
  `no_data_reason` (mirror the findings collectors' silent-when-uncertain contract).

### REST API route (frontend path)

`api/simulation/handler.py` — add a route/branch so the frontend calls it without the
agent (the Aurora simulator panels already do this). Reuse the same `_impl` logic — factor
the pure compute into a shared function imported by both the MCP tool and the REST handler,
OR duplicate the small compute (match the existing simulation REST/MCP split — implementer
checks how Aurora scaling does it and mirrors). Route returns the same JSON shape.

### Frontend: open the simulator for DynamoDB

`frontend/src/app/simulator/page.tsx` — replace the blanket non-relational EmptyState with:

```tsx
fam === "relational"   ? <UpgradePanel/.../>      // unchanged
: fam === "dynamodb"   ? <DynamoDbCapacitySimulator clusterId={selectedCluster} engine={current?.engine}/>
: /* documentdb */       <EmptyState title="DocumentDB 시뮬레이션은 지원 예정" .../>
```

New `frontend/src/components/dashboard/dynamodb-capacity-simulator.tsx` (or under a
`simulator/` folder consistent with the page's other panels):

- Header: current billing mode badge + region.
- A `StatRow` of three `Stat`s: **현재 월 비용** / **On-Demand 월 비용(추정)** / **Provisioned 월 비용(추정)**.
- A recommendation banner: "On-Demand로 전환 시 월 $X (Y%) 절감 예상" / "현재 모드가 최적" —
  only when both prices resolved.
- An assumptions disclosure (p99 sizing, headroom, 730h month, RRU≈RCU approximation).
- Reuse the simulator page's `PricingContext` component (source badge: `aws_pricing_api`
  vs `fallback`, region). Mirror `cost/page.tsx PlatformCostView` for layout/number
  formatting (천 단위 쉼표 via `fmtDecimal` — number-formatting memory rule).
- `no_data` / `partial` / `fallback` states render an `EmptyState` / partial card, never a
  blank or a fake number.
- i18n: Korean for explanations/labels; keep DBA jargon (RCU/WCU/On-Demand/Provisioned/p99)
  in English (translation-scope memory rule).

## Testing

- **Unit** `tests/unit/.../test_dynamodb_pricing.py`: mock `boto3.client("pricing")
.get_products` with a Price List fixture → asserts each of the 4 prices parse from the
  right usagetype suffix; soft-fail (exception) → `None`; cache hit avoids a 2nd call.
- **Unit** `test_simulate_dynamodb_capacity_cost`: mock the cache (`CacheClient.execute`)
  with consumed series + billing_mode + mock pricing → asserts on-demand vs provisioned
  math, p99 sizing, recommendation direction, the `no_data` floor, and the `partial`/
  `fallback` path when a price is `None`. Pure-compute function tested directly.
- **Cedar parity**: the existing `test_tool_schema_parity.py` requires every read-only
  simulation tool to be in `simulation_policy.cedar` — **add the new tool action** to the
  Cedar allowlist + the parity test's `_READONLY_POLICY`, or the parity test fails.
- **CDK**: no new resources → `cdk synth` unchanged (just confirm it still synths).
- **Frontend**: `tsc --noEmit` + `npm run build`.
- **Live**: verify against the kept DynamoDB demo table (`ddb-*`) in the browser — real
  consumed series + real Seoul pricing → a real dollar comparison.

## Out of scope

- Reserved Capacity / free-tier / storage / backup / stream / global-table replication
  cost — capacity (RCU/WCU) only; state this in the assumptions. (A later iteration can add
  storage from `resource_details.table_size_bytes`.)
- Auto-scaling schedule modeling beyond the single headroom factor.
- Actually changing the billing mode — that's a WRITE and belongs with the gated NoSQL
  write/remediation work (the other remaining Group C item).
