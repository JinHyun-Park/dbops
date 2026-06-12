# Simulation + Cost for new engines — Design Spec (program spec #5, final)

- **Date**: 2026-06-12
- **Status**: Proposed
- **Depends on**: #1/#2/#3/#4 (deployed). ADR 2026-06-12: cache-read first-party; no AWS MCP,
  no direct DB connection; writes for new engines out of scope.

## Goal

Make the **Simulator** and **per-cluster cost** features behave correctly for DocumentDB and
DynamoDB. Today both assume Aurora: the Simulator renders four Aurora-only panels (Upgrade /
Parameter / Scaling / DDL) for _any_ selected cluster with no engine gating, the six simulation
MCP tools have **no engine guard** (an agent call on a NoSQL table runs Aurora logic → garbage),
and the cost rightsizing collector (`cost_check.py`) runs relational-only. This spec closes those
gaps the same way #1 closed the diagnosis gaps: **capability gating** + the one genuinely-applicable
parity extension.

## Scope decision (what maps, what doesn't)

The "full parity = meaningful equivalents, skip what doesn't apply" rule from the program
brainstorm, applied to Simulation/Cost:

| Feature                                         | Aurora                                                                   | DocumentDB                                                                         | DynamoDB                                                                                                                         |
| ----------------------------------------------- | ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Version-upgrade sim (compatibility/impact/plan) | ✓                                                                        | engine versions exist but **deferred** (low value, net-new)                        | N/A (serverless, no version)                                                                                                     |
| Parameter-change sim                            | ✓                                                                        | N/A (limited param surface)                                                        | N/A                                                                                                                              |
| DDL-impact sim                                  | ✓                                                                        | N/A (no SQL DDL)                                                                   | N/A                                                                                                                              |
| Scaling/ACU sim                                 | ✓                                                                        | N/A in v1 (instance class change — deferred)                                       | capacity-mode/throughput cost — **deferred** (net-new, pricing-accuracy risk; advice already delivered via #3 finding + #4 chat) |
| CPU-based cost rightsizing finding              | ✓                                                                        | **deferred** (DocDB lacks `instance_class`; CPU metric_type mismatch — see Part 2) | already covered by #3 `ddb_capacity_overprovisioned`                                                                             |
| Account-level Savings Plan finding              | ✓                                                                        | already account-wide (no change)                                                   | already account-wide                                                                                                             |
| Bedrock/AgentCore platform Cost page            | engine-agnostic (Cost Explorer, `Application=DBOps` tag) — **no change** | —                                                                                  | —                                                                                                                                |

**Net**: simulation is an Aurora-specific feature; for NoSQL we gate it off **cleanly** (no broken
panels, no garbage tool runs, agent says so plainly) — that is the genuine correctness need #5
delivers. DynamoDB cost is already handled by #3; the Cost page is platform-Bedrock spend
(engine-agnostic). DocDB cost rightsizing, the DynamoDB capacity-mode cost simulator, and the DocDB
version-upgrade sim are all explicitly deferred (see Part 2 + Out of scope) with concrete reasons.

## Architecture

### Part 1 — Capability gating for Simulation (correctness; mirrors #1's diagnosis gating)

- **Capability key**: add `"simulation": True` to `CAPABILITIES[RELATIONAL]` and `False` to
  `DOCUMENTDB` / `DYNAMODB` in all **4** `engine_family.py` copies (data-pipeline/etl_collector,
  api/clusters, api/dashboard, mcp-servers/shared) — keep them byte-identical (md5-verify).
- **Simulation MCP tools** (`mcp-servers/mcp_servers/simulation/handler.py`): add a shared engine
  guard at dispatch — if the resolved cluster's family has `simulation=False`, return
  `{"status": "unsupported_engine", "engine_family": fam, "message": "..."}` for all six tools,
  **mirroring the `execute_sql` guard** (`isinstance(cluster, dict)` check first, then
  `CAPABILITIES.get(fam, {}).get("simulation", True)` — defaulting True so unknown/mock clusters
  and missing-meta paths don't false-positive, exactly as the execute_sql fix `9520191` did).
  Resolve the cluster via the same lookup the tools already use (cache `cluster_meta` read).
- **Cedar**: simulation tools are **read-only** (verified: no mutation calls). There is currently
  **no `simulation_policy.cedar`** — add one with a permit allowlist of the six tools
  (`check_upgrade_compatibility`, `estimate_upgrade_impact`, `generate_upgrade_plan`,
  `simulate_parameter_change`, `simulate_scaling`, `simulate_ddl_impact`). Extend the read-only
  Cedar parity test (`tests/unit/test_tool_schema_parity.py`) to include the `simulation` server →
  `simulation_policy.cedar`. (Fills a pre-existing gap: simulation had no Cedar policy at all.)
- **Frontend Simulator page** (`frontend/src/app/simulator/page.tsx`): gate the four panels by
  engine family. For a non-relational selected cluster, render an `EmptyState`:
  "시뮬레이션은 Aurora(PostgreSQL/MySQL) 전용입니다 — 업그레이드·파라미터·DDL·스케일링은
  관계형 엔진에만 적용됩니다." Use `engineFamily(current?.engine)` + a `FAMILY_PANELS`/capability
  check from `lib/engine.ts`. Do not render Upgrade/Parameter/Scaling/DDL for documentdb/dynamodb.
- **Agent prompt** (`agent/prompts/system_prompt.py` + `cheatsheet.py`): one line — the simulation
  tools (upgrade/parameter/DDL/scaling) are **Aurora-only**; for DynamoDB capacity/cost or
  DocumentDB scaling questions, use `get_maintenance_findings` and recommend the AWS Console /
  CDK path (no simulation tool call). Validate edits with `ast.parse` ONLY (no python in `agent/`).

### Part 2 — DocumentDB cost rightsizing — **DEFERRED** (data plumbing not in place)

Originally scoped as a "cheap win" by reusing Aurora's `collect_cost_findings`. Investigation
during planning found the Aurora cost collector's data contract does **not** match DocDB's, so a
naive wire-up would ship a **dead feature** (the exact "scaffold runs, emits nothing" trap):

- Aurora `_check_oversized` reads `metric_snapshots WHERE metric_type = 'cpu'`, but DocDB stores
  CPU as `metric_type = 'cpu_utilization'` → the CPU query returns 0 samples →
  `collect_cost_findings` early-returns `insufficient_cpu_history` and `_check_oversized` never runs.
- `_check_oversized` needs the **instance class** (to skip burstable t-family and to name the
  downsize target). DocDB leaves `cluster_meta.instance_class` NULL and its `resource_details`
  stores only instance **identifiers** (`{"instances":[...],"instance_count":N}`) — no class.
- The only live DocDB fixture (`dbops-docdb-test`) is `db.t3.medium` (burstable) → would be
  correctly skipped even with full plumbing, so the win can't be demonstrated.

**Deferred** to a dedicated follow-up that (1) extends `docdb_cw_collector`/meta to capture the
writer instance class into `resource_details`/`cluster_meta`, then (2) adds a DocDB-specific
`docdb_cost_oversized` rule **inside `docdb_findings.py`** (reading `cpu_utilization` + the captured
class), mirroring the per-engine findings pattern of #2/#3 rather than overloading the Aurora
collector. DynamoDB cost rightsizing is already delivered by #3 (`ddb_capacity_overprovisioned`).

**Net #5 ships Part 1 only** (simulation gating) — the genuine correctness need. The Cost page is
platform-Bedrock spend (engine-agnostic, no change). No frontend cost-label change is needed since
no new DocDB finding is emitted.

## Testing

- **Unit**: simulation engine-guard returns `unsupported_engine` for documentdb/dynamodb and runs
  normally (or defaults-permit) for relational/unknown/mock clusters AND when cluster_id is absent
  (mirror the execute_sql guard tests). Cedar parity test now covers the simulation server.
- **Live** (deploy agent + frontend): (1) select the DynamoDB scenario table in the Simulator →
  see the Aurora-only EmptyState, not broken panels; same for `dbops-docdb-test`. (2) Ask the agent
  in chat to "simulate an upgrade" on the DynamoDB table → it declines and points to findings/console
  (no garbage). (3) Confirm the Aurora Simulator is unchanged (all four panels work on a relational
  cluster).
- Full backend unit suite green (lesson: run the FULL suite — per-task subsets have hidden
  regressions). Frontend `tsc --noEmit` + eslint clean. (No data-stack deploy needed — Part 2 cost
  collector work is deferred.)

## Out of scope (explicitly deferred, with reason)

- **DynamoDB capacity-mode cost simulator** (Provisioned↔On-Demand $ what-if): net-new feature
  requiring a region-specific RCU/WCU pricing model that goes stale; a wrong cost number is worse
  than none. The rightsizing **advice** is already delivered by #3's `ddb_capacity_overprovisioned`
  finding and #4 chat. Revisit only with a live Pricing-API-backed estimate.
- **DocumentDB version-upgrade sim** (4.0→5.0 compatibility/impact): low value vs. effort; AWS
  DocDB upgrades are infrequent and the impact model differs from Aurora's. Deferred.
- **DocumentDB instance-class scaling sim**: deferred with the upgrade sim; cost rightsizing
  finding (Part 2) covers the "are you oversized?" question without a what-if calculator.
- **WRITE/remediation tools** for any new engine (capacity change, TTL, index ops) + Cedar/approval
  — the standing follow-up from #4.
