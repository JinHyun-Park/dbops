# DynamoDB Diagnosis — Design Spec (program spec #3)

- **Date**: 2026-06-12
- **Status**: Proposed
- **Depends on**: #1 Foundation (deployed). Decision per ADR 2026-06-12 (AWS-managed MCP):
  **Option A** — first-party bounded reads over the cache + CloudWatch; no AWS MCP.

## Goal

Surface DynamoDB operational findings + recommendations (the "AI 진단/권장" the user noted
were absent for non-relational engines), built the same way as the Aurora finding
collectors (cache-only, conservative, silent-when-uncertain), and shown in a DynamoDB
"진단(Maintenance/Health)" panel.

## Architecture (mirrors the Aurora finding collectors)

- New collector `data-pipeline/etl_collector/collectors/dynamodb_findings.py`, called from
  the handler's `dynamodb` branch in `_collect_one` **sharing `run_ts`** (the MAX(snapshot*time)
  batch invariant — same rule as pg findings). Reads the cache only: `metric_snapshots`
  (the DynamoDB metric_types Foundation already collects) + `cluster_meta.resource_details`
  (billing_mode, provisioned capacity context, gsi/lsi). Emits rows into
  `cluster_health_findings` with `check_type = ddb*\*`.
- Capability gating: add the `ddb_*` finding group to `CAPABILITIES["dynamodb"]["findings"]`
  (currently empty), and un-gate `_health_findings` for the dynamodb family so it returns
  these rows. Frontend: render a Maintenance/Health findings panel on the DynamoDB dashboard
  and add `ddb_*` → label mapping.
- **Per-GSI signal**: Foundation collects table-level metrics only. Extend the DynamoDB
  collector to also pull throttle/consumed with a `GlobalSecondaryIndexName` dimension for
  each GSI in resource*details (bounded), stored as `metric_type` `gsi*<name>\_\*` or with a
  dimensions tag. (Needed for the GSI-health finding.)

## Proposed findings (conservative thresholds — silent when uncertain)

All over a rolling window (default 1h; configurable). Throttle/consumed are 1-min metrics;
provisioned is 5-min. Skip a rule if its inputs are missing/insufficient (mirrors pg rules).

| check_type                      | severity         | signal (threshold)                                                                                                                | recommendation                                                                       |
| ------------------------------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `ddb_throttling`                | warning→critical | SUM(read+write throttle events, throttled_requests) over window > 0; critical if sustained (>N per min across ≥M minutes)         | raise capacity / switch to on-demand / investigate hot key                           |
| `ddb_capacity_underprovisioned` | warning          | PROVISIONED only: peak consumed / provisioned ≥ 80%                                                                               | raise provisioned capacity or enable auto-scaling / on-demand                        |
| `ddb_capacity_overprovisioned`  | info             | PROVISIONED only: peak consumed / provisioned ≤ 20% sustained                                                                     | downsize provisioned or switch to on-demand (cost)                                   |
| `ddb_hot_partition`             | warning          | throttle events > 0 **AND** peak consumed < provisioned × 0.5 (throttling despite table-level headroom ⇒ uneven key distribution) | review partition-key design / add write sharding; (data-plane key sampling deferred) |
| `ddb_gsi_throttling`            | warning          | per-GSI throttle events > 0                                                                                                       | the GSI is under-provisioned or hot — raise GSI capacity / review GSI key            |
| `ddb_ondemand_high_throughput`  | info             | PAY_PER_REQUEST + sustained high consumed (≥ threshold)                                                                           | consider PROVISIONED + auto-scaling for cost at sustained high volume                |

Notes:

- `ddb_hot_partition` is a **CloudWatch-only inference** for v1 (throttle-despite-headroom).
  Actual key-distribution sampling (a bounded boto3 read) is a later refinement, not v1.
- Thresholds are first-pass and meant to be tuned against the live scenario table.

## Testing

- Unit tests for each rule (boundary conditions; silent-when-uncertain) — mock cache reads,
  assert emitted check_types, mirroring `test_param_fitness.py` / `test_capacity_forecast.py`.
- Live: the warm `dbops-ddb-scenario-test` (heavy throttling on 1 RCU/1 WCU, PROVISIONED,
  GSI+LSI) should fire `ddb_throttling`, `ddb_capacity_underprovisioned`, and `ddb_hot_partition`.

## Out of scope (later)

- Data-plane key/item sampling for precise hot-key identification.
- MCP/agent chat diagnosis for DynamoDB (program spec #4).
- Cost dollar estimates (only directional cost guidance here).
