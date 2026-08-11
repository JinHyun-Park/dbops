# APM for EC2 Java/Spring Boot — Design

**Date:** 2026-08-11
**Status:** Approved (brainstorming), pending implementation plan
**Scope:** New bottom-menu "APM" feature that monitors Java/Spring Boot applications
running on EC2 — application/console logs (primary), APM metrics, and host metrics.

## 1. Goal & Non-Goals

### Goal

Add an **APM** feature so DBAs can monitor Java/Spring Boot servers deployed on EC2
from within dbops, following existing dbops patterns (cache-first reads, cross-account
spoke-role chaining, read-only least-privilege). The feature surfaces:

- **Application/console logs** (the primary ask) — on-demand search with a log-level
  filter (ERROR/WARN/INFO/DEBUG), free-text, and time range.
- **APM metrics** — latency, error rate, request rate, JVM runtime (via CloudWatch
  Application Signals), read from cache.
- **Host metrics** — EC2 CPU/memory/disk (`AWS/EC2` + `CWAgent`), read from cache.

### Non-Goals (YAGNI)

- dbops does **not** install/configure any instrumentation on EC2. The user is assumed
  to have set up the CloudWatch Agent + ADOT (for Application Signals) beforehand.
  dbops is **read-only** against CloudWatch.
- No storage of raw log lines in the cache (only per-level counts are aggregated).
- No Application Signals ENFORCE, no SNS/alerting integration, no distributed-tracing
  detail view. These may come later.
- Does not touch the five `engine_family.py` copies — APM targets live in a separate
  registry.

## 2. Key Decisions (from brainstorming)

| Decision | Choice |
| --- | --- |
| 1st-scope | All three axes: logs + APM metrics + host metrics |
| Instrumentation | User pre-installs (CloudWatch Agent/ADOT); dbops is read-only |
| Target registration | Explicit APM-target registration in a **new** DynamoDB registry (not the clusters registry, not `engine_family`) |
| Landing screen | Log-centric + metric summary |
| Log search | Level filter is a first-class parameter; default **ERROR+WARN** to avoid unbounded scans |
| Metrics vs logs | **Metrics = cache-first; log search = on-demand** against CloudWatch |

## 3. Architecture & Data Flow

dbops's rule holds: never call AWS in real time for dashboard rendering. Metrics are
pre-collected into the Aurora PG cache. The one deliberate exception is **on-demand log
search** — logs are too large to replicate into the cache, and this mirrors the existing
`api/dashboard` `_log_insights` route that already queries CloudWatch Logs on demand.

### Target registration (new registry)

New DynamoDB table `apm_targets`, separate from the `clusters` registry so the
`engine_family` taxonomy is untouched. Item shape:

- `target_id` (PK), `instance_id`, `region`, `account_id`, `spoke_role_arn`
- `log_groups` (list — the CloudWatch log groups for this app)
- `service_name` (for Application Signals)
- `team` (tenancy scoping)

### Collection (pull → cache)

```
EventBridge schedule
  → etl_collector Lambda
     → scan apm_targets
     → per target: assume spoke_role_arn (existing get_client pattern)
     → pull from CloudWatch (read-only):
        · host metrics: GetMetricData  (AWS/EC2 + CWAgent)
        · APM metrics:  Application Signals + GetMetricData (latency/error/req/JVM)
        · log levels:   Logs Insights `stats count() by level` (recent bucket only)
     → write to Aurora PG cache (new tables)
```

### Query (2 paths)

- Dashboard/metrics/log-summary: `GET /api/apm/...` → Lambda → Aurora PG cache.
- **On-demand log search only**: `POST /api/apm/{target}/logs/search` → Lambda assumes
  the spoke role and calls CloudWatch Logs `FilterLogEvents` / Logs Insights
  `StartQuery`+`GetQueryResults` at that moment. Level filter applied server-side.

## 4. Data Model (Aurora PG cache)

Added in `data-pipeline/schema_migrator/sql/schema_v28.sql`, following existing
conventions (`TIMESTAMPTZ`, JSONB, `ON CONFLICT` idempotent inserts). The migrator
auto-picks up the new file (dir SHA changes → Custom Resource re-runs on `cdk deploy`).

```sql
-- 1) APM target meta (convenience mirror; source of truth is DynamoDB apm_targets)
CREATE TABLE IF NOT EXISTS apm_target_meta (
  target_id     VARCHAR(255) PRIMARY KEY,
  instance_id   VARCHAR(64),
  region        VARCHAR(32),
  service_name  VARCHAR(255),
  log_groups    JSONB,
  team          VARCHAR(255),
  last_seen_at  TIMESTAMPTZ
);

-- 2) Metric snapshots (host + APM metrics, mirrors metric_snapshots pattern)
CREATE TABLE IF NOT EXISTS apm_metric_snapshots (
  target_id    VARCHAR(255),
  ts           TIMESTAMPTZ,
  metric_type  VARCHAR(64),   -- cpu, mem, latency_p99, error_rate, req_rate, jvm_heap ...
  value        DOUBLE PRECISION,
  dimensions   JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_apm_metric_lookup
  ON apm_metric_snapshots (target_id, metric_type, ts);

-- 3) Log-level counts (periodic aggregation; raw log lines are NOT stored)
CREATE TABLE IF NOT EXISTS apm_log_level_counts (
  target_id  VARCHAR(255),
  ts         TIMESTAMPTZ,     -- 1m/5m bucket
  log_group  VARCHAR(512),
  level      VARCHAR(16),     -- ERROR / WARN / INFO ...
  count      BIGINT,
  UNIQUE (target_id, ts, log_group, level)
);
```

**Key decision:** raw log lines are never stored. The cache holds only per-level counts;
actual log lines are fetched on demand from CloudWatch at search time (better cost,
capacity, and security posture).

## 5. Backend Components

### New REST route group — `api/apm/handler.py`

Mirrors the `api/saved_queries/handler.py` skeleton (`_resp()` CORS helper, JWT decode,
tenancy scoping, `_execute()` RDS Data API wrapper).

| Route | Methods | Source | Purpose |
| --- | --- | --- | --- |
| `/api/apm/targets` | GET, POST | DynamoDB `apm_targets` | list / register |
| `/api/apm/targets/{id}` | GET, PUT, DELETE | DynamoDB | manage |
| `/api/apm/{id}/overview` | GET | Aurora PG cache | summary cards (latency/error/CPU/mem + error-log counts) |
| `/api/apm/{id}/metrics` | GET | Aurora PG cache | time series (metric_type, range) |
| `/api/apm/{id}/logs/search` | POST | **on-demand** CloudWatch Logs | level-filtered + text + time-range search |

Log-search handler reuses the `api/dashboard/handler.py` `_log_insights` logic
(spoke role → `start_query`/`get_query_results`). Request body:
`{ levels: ["ERROR","WARN"], query: "...", start, end, limit }`. Levels are translated
server-side into a Logs Insights `filter` clause; **when `levels` is omitted the default
is ERROR+WARN** so no request scans all logs unbounded.

### New collector — `data-pipeline/etl_collector/collectors/apm_collector.py`

- Scans `apm_targets`; per target assumes the spoke role (existing `get_client` pattern).
- `GetMetricData` for host/APM metrics → `apm_metric_snapshots`.
- Logs Insights `stats count() by level` (recent bucket) → `apm_log_level_counts`.
- Registered in `data-pipeline/etl_collector/handler.py`. Because APM targets are a
  separate registry (not the clusters registry), `lambda_handler` gains a dedicated APM
  collection pass rather than being wired into the per-cluster `_collect_one`.

### IAM / cross-account

- New route Lambda + collector Lambda: cache Data API access, `apm_targets` read, and
  permission to assume the spoke role.
- `cdk/cross-account/spoke-role-template.yaml`: add **read-only** CloudWatch permissions —
  `cloudwatch:GetMetricData`, `logs:StartQuery`/`GetQueryResults`/`FilterLogEvents`,
  `application-signals:*` (read), and `ec2:DescribeInstances` for target verification.
  All read-only. **User must redeploy the spoke role** in each target account.

## 6. Frontend

### Menu — `frontend/src/components/app-shell.tsx`

- Append to the **Configure** group after `/health`:
  `{ href: "/apm", label: "APM", icon: Activity, hint: "EC2 앱 로그·성능 모니터링" }`.
- Add `apm: "APM"` to the `humanize` map.

### Page — `frontend/src/app/apm/page.tsx` (`"use client"`)

- Top: APM-target selector (its own selector, separate from the cluster dropdown;
  `fetchApmTargets()`).
- Metric summary cards (`PageHeader` + `Stat`/`StatRow`): latency p99, error rate,
  request rate, CPU/memory, error-log count → `fetchApmOverview()` + `useSmartPoll` 15s.
- Mini time series (reuse existing dashboard chart components).
- **Log viewer (the centerpiece)**: level-filter toggles (ERROR/WARN/INFO/DEBUG,
  default ERROR+WARN) + text search + time range → `searchApmLogs()` on-demand POST.
  Results render as timestamp + level badge + message.
- When no target is registered: `EmptyState` prompting registration.

### API client — `frontend/src/lib/api-client.ts`

Add `fetchApmTargets`, `createApmTarget`, `fetchApmOverview`, `fetchApmMetrics`,
`searchApmLogs` following the existing `authedFetch` pattern. (Note: data fetching uses
`useSmartPoll` + api-client, **not** TanStack Query, which is not present in the codebase.)

## 7. Testing

- `tests/unit/`: apm handler (route dispatch, tenancy, level→filter translation with the
  ERROR+WARN default); collector (GetMetricData/Insights aggregation → cache write) with
  boto3 stub/mock.
- `tests/cdk/`: all four stacks synth (including new Lambda/route/table).
- `tests/unit/test_openapi_spec.py`: regenerate `frontend/public/openapi.json` via
  `python tools/openapi_gen.py` after adding routes.
- Frontend: `npm run build` (static export) + direct browser test on completion
  (project hard rule).

## 8. Files Touched

- **New:** `api/apm/handler.py` (+`__init__.py`), `data-pipeline/etl_collector/collectors/apm_collector.py`,
  `data-pipeline/schema_migrator/sql/schema_v28.sql`, `frontend/src/app/apm/page.tsx`.
- **Modified:** `cdk/stacks/agent_stack.py` (Lambda + routes + IAM), `cdk/stacks/foundation_stack.py`
  (`apm_targets` DynamoDB table), `cdk/cross-account/spoke-role-template.yaml` (read-only CW),
  `data-pipeline/etl_collector/handler.py` (APM collection pass), `frontend/src/components/app-shell.tsx`,
  `frontend/src/lib/api-client.ts`, `frontend/public/openapi.json` (regenerated).
</content>
</invoke>
