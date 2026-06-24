# ElastiCache EC-2 — Read Diagnosis / Findings — Design

**Date:** 2026-06-24
**Status:** approved (EC-2 scope locked in the user-approved 5-spec ElastiCache program; design decisions per the "continue" directive)

## Context

Second spec of the ElastiCache engine-family program (see
`2026-06-24-elasticache-foundation-design.md`). EC-1 made ElastiCache
registered + CloudWatch-collected + dashboard-visible (read-only). EC-2 adds
**diagnosis**: it analyzes the cached metrics into health _findings_ (eviction
pressure, low hit-rate, memory pressure, replication lag, high CPU, connection
surge) and feeds ElastiCache-specific signals into incident root-cause analysis.

The findings + RCA pipelines are already multi-engine and engine-agnostic at the
surface, so EC-2 is purely additive: a new findings collector (mirroring
`dynamodb_findings.py` / `docdb_findings.py`), its ETL dispatch wiring, and a new
RCA signal source. No new API route or MCP tool is needed — findings flow through
the existing engine-agnostic `_health_findings` endpoint
(`GET /api/dashboard?page=health`) and the `get_maintenance_findings` MCP tool,
which read `cluster_health_findings` for any cluster regardless of engine.

`CAPABILITIES["elasticache"]["findings"] == {"elasticache"}` was already declared
in EC-1; EC-2 fills it with real `elasticache_*` finding rows.

This spec covers **EC-2 only**. Non-goals: live protocol deep-read (EC-3),
write tools (EC-4), simulation/cost (EC-5), new dashboard panels (EC-1 shipped
the metric panels; findings render via the existing health panel).

## Architecture

Three additive components, all read-only over the existing Aurora PG cache.

### Component 1 — `data-pipeline/etl_collector/collectors/elasticache_findings.py` (new)

Mirrors `dynamodb_findings.py` / `docdb_findings.py`:

- Signature: `collect_elasticache_findings(cache_rds_data, cache_cluster_arn,
cache_secret_arn, cache_db_name, cluster_id, snapshot_ts=None, window_hours=1) -> dict`.
- `ts = snapshot_ts or datetime.now(timezone.utc).isoformat()` — but the handler
  ALWAYS passes `snapshot_ts=run_ts` (the shared-timestamp gotcha: all findings in
  one ETL cycle must share `snapshot_time` or the dashboard `MAX(snapshot_time)`
  batch drops them).
- Reads `cluster_meta.engine` (redis/valkey/memcached) to branch the hit-rate
  metric keys, then aggregates the last `window_hours` of `metric_snapshots` via
  the same `_execute(rds_data, cluster_arn, secret_arn, db_name, sql, params)`
  helper the sibling collectors use.
- Computes findings, then INSERTs each into `cluster_health_findings`
  `(cluster_id, snapshot_time, check_type, severity, subject, value_str,
threshold_str, recommendation, details)` — `details` is JSONB for AI-explain.
- Returns `{"cluster_id": ..., "findings_emitted": N, "errors": [...]}`; never
  raises out (the handler also wraps it).

**The six finding rules** (`check_type` is `elasticache_*`-prefixed; severities
critical|warning|info; Korean `value_str`/`threshold_str`/`recommendation`):

| check_type                     | rule (window = `window_hours`, default 1h)                                                                                                                                                | severity         |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `elasticache_evictions_spike`  | `SUM(evictions)` over window: `>1000` → critical, `>100` → warning                                                                                                                        | critical/warning |
| `elasticache_low_hit_rate`     | hit-rate = `SUM(hits)/(SUM(hits)+SUM(misses))` (Redis: cache_hits/cache_misses; Memcached: get_hits/get_misses); with ≥20 samples: `<70%` → critical, `<85%` → warning                    | critical/warning |
| `elasticache_memory_pressure`  | `MAX(memory_usage_pct)`: `≥95` → critical, `≥85` → warning (Redis/Valkey only — Memcached has no DatabaseMemoryUsagePercentage; skip)                                                     | critical/warning |
| `elasticache_replication_lag`  | `MAX(replication_lag)` ms: `≥1000` → critical, `≥100` → warning (Redis/Valkey only; skip for Memcached)                                                                                   | critical/warning |
| `elasticache_high_cpu`         | `MAX(engine_cpu)` (fallback cache_cpu): `≥90` → critical, `≥80` → warning. EngineCPU is the single-threaded Redis bottleneck, so prefer it                                                | critical/warning |
| `elasticache_connection_surge` | `MAX(curr_connections)` vs a soft ceiling: `>60000` → warning (Redis hard cap is 65000); else if `SUM(new_connections)` over window shows a >3× jump vs the window's median minute → info | warning/info     |

Each rule emits at most one finding (highest severity tier reached). A rule with
insufficient data (empty series / <min samples) emits nothing. Memcached-only
clusters skip the replication-lag + memory-pressure rules and use
`get_hits`/`get_misses` for hit-rate.

### Component 2 — ETL handler dispatch (`data-pipeline/etl_collector/handler.py`)

In `_collect_one`, the existing `if family == "elasticache":` branch (EC-1)
currently collects metrics then `return result`. EC-2 adds the findings call
BEFORE the return, mirroring the dynamodb/docdb branches:

```python
        try:
            result["elasticache_findings"] = collect_elasticache_findings(
                cache_rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
                cluster_id, snapshot_ts=run_ts,
            )
        except Exception as e:
            result["elasticache_findings_error"] = str(e)
            print(f"[{cluster_id}] elasticache findings error: {e}")
```

Plus the import. `snapshot_ts=run_ts` is mandatory (shared-timestamp gotcha).

### Component 3 — RCA signal source (`mcp-servers/mcp_servers/incident/tools/diagnose_root_cause.py`)

Add `_collect_elasticache_signals(cache, cluster_id, start_iso, end_iso, anchor,
win, examined, skipped)` and call it in `diagnose_root_cause_impl` alongside the
existing `candidates.extend(_collect_*())` calls (lines ~182-188). It reads
`metric_snapshots` (engine-agnostic table) for cache-specific spikes the generic
`_collect_metric_spikes` does not semantically capture:

- **Eviction spike** — `evictions` datapoints over a per-minute threshold within
  the window; category `elasticache_spike`, severity info.
- **Replication-lag spike** — `replication_lag` crossing the warning threshold;
  treated as more event-like (higher weight) since lag often coincides with a
  failover/load event.
- **Hit-rate drop** — compare hit-rate in the window vs the baseline window
  (`baseline_start_iso`); a >15% relative drop is a strong cache-incident signal.

Add a `BASE_WEIGHTS["elasticache_spike"]` entry (e.g. `2.5` — between
metric_spike 2.0 and blocking 3.0, since a cache-specific spike is more
diagnostic than a generic metric blip but less than lock contention). Each
candidate carries the standard shape (`when, category, title, description,
severity, score, score_breakdown`). The source is wrapped in try/except → on any
failure it appends to `skipped` and returns `[]` (matching every sibling source),
so it is safe for non-ElastiCache clusters (their `metric_snapshots` simply have
no eviction/replication_lag rows → no candidates).

## Data Flow

ETL cycle → `collect_elasticache_findings` reads `metric_snapshots` +
`cluster_meta` → writes `cluster_health_findings` (shared `snapshot_time`). On
read: the existing `_health_findings` endpoint + `get_maintenance_findings` MCP
tool return the `elasticache_*` rows unchanged (no engine filter). RCA: the agent
calls `diagnose_root_cause`, which now includes ElastiCache signal candidates.

## Error Handling

- Collector: each rule guarded; missing/empty series → no finding (never raise).
  A DB read failure appends to `errors` and returns a partial result; the handler
  try/except isolates it from the rest of the ETL cycle.
- RCA signal source: try/except → `skipped` + `[]` on any failure (engine-safe).

## Testing

- **Collector unit** (`tests/unit/data_pipeline/test_elasticache_findings.py`),
  mocking the `_execute` cache reads: each of the 6 rules fires at its threshold
  and is silent below it; Redis vs Memcached branch (Memcached skips
  replication-lag + memory-pressure and uses get_hits/get_misses); hit-rate
  divide-by-zero guard; the collector passes `snapshot_ts` through to every
  INSERT (shared-timestamp); insufficient-samples → no finding.
- **ETL dispatch unit**: `family=="elasticache"` now also calls the findings
  collector with `snapshot_ts=run_ts`; a findings error does not stop metrics.
- **RCA unit** (`tests/unit/mcp_servers/incident/...`): `_collect_elasticache_signals`
  returns eviction/replication/hit-rate-drop candidates from mocked metric rows;
  returns `[]` (and records `skipped`) when the table read fails; the new
  `BASE_WEIGHTS["elasticache_spike"]` is applied to the score.
- Full unit suite green; CDK synth green (no infra change expected — collector is
  packaged with the ETL Lambda; verify no new IAM needed since it only reads the
  Aurora cache via the existing Data API path).

## Security

- Fully read-only: reads the Aurora PG cache (`metric_snapshots`, `cluster_meta`)
  and writes only the internal `cluster_health_findings` table. No ElastiCache
  API calls, no mutation, no new IAM, no secret, no protocol connection.
- Findings surface through the existing admin/authz-gated dashboard + MCP paths
  (unchanged).
