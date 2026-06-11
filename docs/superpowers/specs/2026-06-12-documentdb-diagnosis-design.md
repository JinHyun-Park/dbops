# DocumentDB Diagnosis — Design Spec (program spec #2)

- **Date**: 2026-06-12
- **Status**: Proposed
- **Depends on**: #1 Foundation (deployed). ADR 2026-06-12: **Option A** for v1 — findings from
  the AWS/DocDB CloudWatch metrics we already collect; **no Mongo connectivity in v1**.

## Goal

DocumentDB operational findings + recommendations, mirroring the Aurora/DynamoDB finding
collectors (cache-only, conservative, silent-when-uncertain), surfaced in the DocumentDB
dashboard's Maintenance Health panel (the capability-gated `_health_findings` already
supports any family with a non-empty findings set).

## Architecture (mirrors dynamodb_findings.py / pg collectors)

- New `data-pipeline/etl_collector/collectors/docdb_findings.py`, called from the handler's
  `documentdb` branch in `_collect_one` **sharing `run_ts`**. Cache-only: reads the AWS/DocDB
  `metric_snapshots` (db*connections, replica_lag_ms, cursors, cursors_timed_out,
  buffer_cache_hit, cpu_utilization, freeable_memory, opcounter*_, read/write*latency_ms,
  disk_queue_depth) + `cluster_meta`. Emits `cluster_health_findings` rows, `check_type = docdb*_`.
- **Collector extension** (`docdb_cw_collector.py`): also collect `DatabaseConnectionsLimit`
  (instance-scoped, writer DBInstanceIdentifier) → metric_type `db_connections_limit`, so
  connection saturation = db_connections / limit.
- Surfacing: `CAPABILITIES["documentdb"]["findings"] = {"docdb"}` (4 engine*family.py copies);
  `_health_findings` already returns findings for any non-empty-capability family. Frontend:
  render the Maintenance Health panel for the documentdb family with `docdb*\*` labels + tabs.

## Proposed findings (conservative; silent when inputs missing)

| check_type                    | severity         | signal (threshold)                                                                   | recommendation                                                                                    |
| ----------------------------- | ---------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| `docdb_connection_saturation` | warning→critical | peak db_connections / db_connections_limit ≥ 80% (crit ≥ 95%); skip if limit unknown | connection pooling / raise instance class / check leaks                                           |
| `docdb_replica_lag`           | warning→critical | peak replica_lag_ms ≥ 1000 (crit ≥ 10000), sustained                                 | reduce write load / scale readers / investigate long-running ops                                  |
| `docdb_cursor_timeout`        | warning          | SUM(cursors_timed_out) over window > 0 (sustained)                                   | app not closing cursors / slow queries holding cursors — review query patterns + cursor lifecycle |
| `docdb_low_cache_hit`         | warning          | avg buffer_cache_hit < 95% (enough samples)                                          | working set exceeds instance memory — raise instance class (Aurora-style: memory-bound)           |

(CPU/opcounter signals: collected + shown on the overview panel, but no finding in v1 — too
generic without a baseline.)

## Out of scope (later)

- **Mongo-protocol deep diagnosis** (`serverStatus`, `currentOp`, slow-op/profiler) — needs the
  A-vs-B connectivity decision from the ADR (thin pymongo read collector in a VPC Lambda vs AWS
  DocDB MCP read-only with credential-level least-privilege). Deferred to a follow-up.
- MCP/agent chat diagnosis for DocumentDB (program spec #4).

## Testing

- Unit tests per rule (boundaries; silent-when-uncertain), mirroring test_dynamodb_findings.py.
- Live: `dbops-docdb-test` (idle single-instance) — most rules correctly silent; drive a few
  connections / verify replica-lag-silent (single instance). Validate the panel renders + the
  collector runs without error; thresholds tuned against live metrics.
