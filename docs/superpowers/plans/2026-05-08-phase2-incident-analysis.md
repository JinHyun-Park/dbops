# Phase 2: Incident Analysis + Advanced Analytics — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 장애 발생 시 AI가 RCA(Root Cause Analysis) 수행 + 분석 리포트 자동 생성 + 3-Tier Knowledge 완성

**Architecture:** Incident MCP Server(6 tools) + Performance 분석 도구 추가(4 tools) + Event Processor Lambda + Report Pipeline + Query Lab UI + AWS Knowledge MCP 연동

**Tech Stack:** Python, Strands SDK, CDK, Lambda, EventBridge, DynamoDB, Aurora PG Cache, Next.js

**Spec:** `docs/superpowers/specs/2026-05-08-dbops-design.md` (sections 5.2-5.3, 4.3)

---

## File Structure (Phase 2 additions)

```
mcp-servers/mcp_servers/
├── performance/tools/
│   ├── detect_anomalies.py        (NEW)
│   ├── detect_regressions.py      (NEW)
│   ├── forecast_capacity.py       (NEW)
│   └── performance_summary.py     (NEW)
├── incident/
│   ├── __init__.py                (NEW)
│   ├── handler.py                 (NEW)
│   └── tools/
│       ├── __init__.py            (NEW)
│       ├── health_status.py       (NEW)
│       ├── recent_events.py       (NEW)
│       ├── search_logs.py         (NEW)
│       ├── correlate_signals.py   (NEW)
│       ├── incident_summary.py    (NEW)
│       └── similar_incidents.py   (NEW)
data-pipeline/
├── event_processor/
│   ├── handler.py                 (NEW)
│   └── requirements.txt           (NEW)
├── report_generator/
│   ├── handler.py                 (NEW)
│   └── requirements.txt           (NEW)
data-pipeline/sql/
│   └── schema_v2.sql              (NEW - additional tables)
api/reports/
│   └── handler.py                 (NEW)
frontend/src/
├── app/query-lab/page.tsx         (NEW)
├── app/reports/page.tsx           (NEW)
├── components/query-lab/
│   └── query-editor.tsx           (NEW)
└── components/reports/
    └── report-viewer.tsx          (NEW)
cdk/stacks/
├── data_stack.py                  (MODIFY - add event processor, report generator)
└── agent_stack.py                 (MODIFY - add incident MCP, reports API)
tests/unit/mcp_servers/
├── incident/                      (NEW - 6 test files)
└── performance/
    ├── test_detect_anomalies.py   (NEW)
    ├── test_detect_regressions.py (NEW)
    ├── test_forecast_capacity.py  (NEW)
    └── test_performance_summary.py(NEW)
```

---

## Task 1: Schema V2 + Event History Tables

**Files:**
- Create: `data-pipeline/sql/schema_v2.sql`

- [ ] **Step 1: Create additional schema**

```sql
-- data-pipeline/sql/schema_v2.sql

-- Event history (stored in Aurora PG Cache for correlation analysis)
CREATE TABLE IF NOT EXISTS event_log (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    source VARCHAR(50) NOT NULL,
    message TEXT,
    severity VARCHAR(20) DEFAULT 'info',
    raw_event JSONB
);

CREATE INDEX idx_event_log_lookup ON event_log (cluster_id, event_time);
CREATE INDEX idx_event_log_type ON event_log (event_type, event_time);

-- Report metadata
CREATE TABLE IF NOT EXISTS reports (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255),
    report_type VARCHAR(50) NOT NULL,
    report_date DATE NOT NULL,
    summary TEXT,
    data JSONB,
    s3_key TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_reports_lookup ON reports (cluster_id, report_type, report_date);
```

- [ ] **Step 2: Commit**

```bash
git add data-pipeline/sql/schema_v2.sql
git commit -m "feat: add schema v2 with event_log and reports tables"
```

---

## Task 2: Performance Analysis Tools (4 tools, TDD)

**Files:**
- Create: `mcp-servers/mcp_servers/performance/tools/detect_anomalies.py`
- Create: `mcp-servers/mcp_servers/performance/tools/detect_regressions.py`
- Create: `mcp-servers/mcp_servers/performance/tools/forecast_capacity.py`
- Create: `mcp-servers/mcp_servers/performance/tools/performance_summary.py`
- Test: 4 test files in `tests/unit/mcp_servers/performance/`

- [ ] **Step 1: Write tests for all 4 tools**

`detect_anomalies` — Takes recent metrics, computes z-score against 7-day baseline, returns anomalous metrics.
`detect_regressions` — Compares query performance before/after a given timestamp, returns queries that degraded.
`forecast_capacity` — Linear regression on storage/connections over N days, returns projected limit date.
`performance_summary` — Aggregates KPIs (avg AAS, top waits, slow query count, peak connections) for a period.

Each tool takes a `CacheClient` and `cluster_id`, queries Aurora PG Cache, and returns structured results.

- [ ] **Step 2: Implement all 4 tools**

Each `_impl` function runs SQL against the cache. Anomaly detection uses `AVG()` and `STDDEV()`. Regression uses two-period comparison on `query_stats`. Forecast uses simple linear regression on `metric_snapshots`. Summary aggregates from multiple tables.

- [ ] **Step 3: Update Performance MCP handler to include new tools**

Add all 4 to the `TOOLS` dict in `mcp-servers/mcp_servers/performance/handler.py`.

- [ ] **Step 4: Run tests**

Expected: 12+ tests PASS (8 existing + 4 new)

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: add performance analysis tools (anomaly, regression, forecast, summary)"
```

---

## Task 3: Incident MCP Server (6 tools, TDD)

**Files:**
- Create: `mcp-servers/mcp_servers/incident/` directory with handler + 6 tools
- Test: 6 test files

- [ ] **Step 1: Write tests for all 6 tools**

`get_health_status` — Queries cluster_meta + recent metric_snapshots for health overview.
`get_recent_events` — Queries event_log table for recent events.
`search_logs` — Calls CloudWatch Logs Insights API (mocked in tests).
`correlate_signals` — Joins metric_snapshots + event_log on time axis, returns timeline.
`get_incident_summary` — Aggregates event_log by type, computes MTTR.
`find_similar_incidents` — Calls Bedrock KB retrieve (mocked in tests).

- [ ] **Step 2: Implement all 6 tools**

- [ ] **Step 3: Create Incident MCP handler**

Same pattern as Performance handler: TOOLS dict, tools/list, tools/call.

- [ ] **Step 4: Run tests**

Expected: 18+ tests PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: add Incident MCP Server with 6 tools (health, events, logs, correlation, summary, similar)"
```

---

## Task 4: Event Processor Lambda

**Files:**
- Create: `data-pipeline/event_processor/handler.py`
- Create: `data-pipeline/event_processor/requirements.txt`
- Modify: `cdk/stacks/data_stack.py` (add event processor Lambda + EventBridge rules)

- [ ] **Step 1: Implement event processor**

Lambda triggered by EventBridge rules for:
- RDS events (failover, maintenance, error)
- CloudWatch Alarm state changes
- Aurora cluster events

Stores events in Aurora PG Cache `event_log` table + sends SNS notification.

- [ ] **Step 2: Update Data Stack CDK**

Add event processor Lambda, EventBridge rules for RDS/CloudWatch events, SNS topic.

- [ ] **Step 3: Commit**

```bash
git commit -m "feat: add Event Processor Lambda with EventBridge rules for RDS/CW events"
```

---

## Task 5: Report Generator Lambda

**Files:**
- Create: `data-pipeline/report_generator/handler.py`
- Create: `data-pipeline/report_generator/requirements.txt`
- Create: `api/reports/handler.py`

- [ ] **Step 1: Implement report generator**

Lambda triggered by EventBridge schedule (daily 9am KST):
1. Calls Performance MCP tools (summary, anomalies, regressions)
2. Calls AgentCore Runtime to generate natural language report
3. Stores in `reports` table + S3

- [ ] **Step 2: Implement reports API**

GET /api/reports — list reports by cluster
GET /api/reports/{id} — get specific report

- [ ] **Step 3: Update CDK stacks**

Add report generator Lambda + schedule to Data Stack.
Add reports API route to Agent Stack.

- [ ] **Step 4: Commit**

```bash
git commit -m "feat: add Report Generator Lambda and Reports API"
```

---

## Task 6: Frontend — Query Lab + Reports Pages

**Files:**
- Create: `frontend/src/app/query-lab/page.tsx`
- Create: `frontend/src/components/query-lab/query-editor.tsx`
- Create: `frontend/src/app/reports/page.tsx`
- Create: `frontend/src/components/reports/report-viewer.tsx`

- [ ] **Step 1: Build Query Lab page**

SQL editor with EXPLAIN button, results panel, AI analysis panel. Uses AgentCore SSE for AI interaction.

- [ ] **Step 2: Build Reports page**

Report list with date filter, report viewer with rendered markdown + charts.

- [ ] **Step 3: Update navigation**

Add Query Lab and Reports to the app layout navigation.

- [ ] **Step 4: Verify build**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: add Query Lab and Reports pages"
```

---

## Task 7: Final Verification + Push

- [ ] **Step 1: Run all tests**
- [ ] **Step 2: Verify CDK synth**
- [ ] **Step 3: Verify frontend build**
- [ ] **Step 4: Push to GitHub**
