# Remediation Outcome Loop (효과 학습 루프) — Design

**Date:** 2026-06-30
**Status:** Approved design, pre-implementation
**Topic:** Close the recommendation loop — measure whether a recommended remediation
actually resolved the symptom, accumulate per-cluster/symptom/action success rates,
and feed that evidence back into future recommendations.

---

## 1. Purpose & framing

DBOps already _produces_ recommendations from three places — proactive anomaly
alerts (`proactive_monitor`), recurring health findings (`cluster_health_findings`),
and RCA task narratives (`task_worker._run_rca`). What it does **not** do today is
learn from outcomes: a recommendation goes out and nothing watches whether the
symptom actually resolved, so the next recommendation is no smarter than the first.

This feature closes that loop:

```
권장 emit (finding / anomaly / RCA)  →  case OPEN
        │ (evaluation window W)
outcome_evaluator (scheduled)  →  automatic verdict (no human input)
        ↓
case status + aggregate store + attribution hint (from event_log / approvals)
        ↓
consumers:  findings re-rank + confidence badge   │   RCA/chat prompt injection   │   Learning UI
```

### Design principles (decided)

- **Core = outcome verification + learned memory.** Not just noise reduction.
- **Fully automatic capture & measurement.** Zero human input — no "was this helpful?"
  prompts. The verdict is derived from observed signals.
- **Measurement is anchored to the triggering symptom, not a blind metric scan.**
  Only the metric / finding that _caused_ the recommendation is watched. This is what
  makes "fully automatic" tractable: it bounds attribution noise.
- **Success signal = baseline-recovery (metric-backed) + finding-recurrence-clearance
  (derived findings), combined** into one verdict per case.
- **Feedback = shared outcome store + deterministic re-rank/confidence + LLM prompt
  injection** (the latter in phase 2).
- **Attribution is a hint, never a causal claim.** See §7.

---

## 2. Data model

Three new tables on the Aurora PG cache (`schema_v24.sql`; the migrator auto-applies
`schema_v*.sql` in numeric order). All timestamps `timestamptz`.

### 2.1 `remediation_cases` — one open case per live symptom

```sql
CREATE TABLE IF NOT EXISTS remediation_cases (
    case_id            BIGSERIAL PRIMARY KEY,
    cluster_id         VARCHAR(255) NOT NULL,
    opened_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- normalized symptom identity
    symptom_class      VARCHAR(80)  NOT NULL,  -- 'anomaly:cpu' | 'finding:query_regression' | 'rca:<category>'
    symptom_subject    VARCHAR(255) NOT NULL DEFAULT '',  -- metric_type | query_hash | param name | ''
    watch_metric       VARCHAR(80),            -- metric_type to evaluate recovery against; NULL = recurrence-only
    severity_at_open   VARCHAR(20),
    -- the recommendation that was emitted
    recommendation_text TEXT,
    action_class       VARCHAR(40)  NOT NULL DEFAULT 'manual',  -- index_add|param_change|scale_up|vacuum|analyze|manual|...
    source             VARCHAR(40)  NOT NULL,  -- 'proactive_monitor' | 'finding_collector' | 'rca_worker'
    -- lifecycle
    status             VARCHAR(20)  NOT NULL DEFAULT 'open',    -- open | resolved | persisted | inconclusive
    evaluate_after     TIMESTAMPTZ  NOT NULL,  -- opened_at + window(symptom_class)
    evaluated_at       TIMESTAMPTZ,
    details            JSONB        NOT NULL DEFAULT '{}'::jsonb  -- attribution hints, baseline snapshot, etc.
);

-- At most one OPEN case per (cluster, symptom_class, subject). Re-emission while a
-- case is open just bumps last_seen_at; it does NOT open a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS ux_remediation_cases_open
    ON remediation_cases (cluster_id, symptom_class, symptom_subject)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS ix_remediation_cases_due
    ON remediation_cases (status, evaluate_after);
```

### 2.2 `remediation_outcomes_agg` — the learned memory

```sql
CREATE TABLE IF NOT EXISTS remediation_outcomes_agg (
    cluster_id      VARCHAR(255) NOT NULL,   -- '*' = fleet-wide rollup (cold-start prior)
    symptom_class   VARCHAR(80)  NOT NULL,
    action_class    VARCHAR(40)  NOT NULL,
    attempts        INTEGER      NOT NULL DEFAULT 0,
    successes       INTEGER      NOT NULL DEFAULT 0,
    last_outcome    VARCHAR(20),             -- resolved | persisted | inconclusive
    last_success_at TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, symptom_class, action_class)
);
```

Every resolved/persisted case increments **two** rows: the cluster-specific row and
the `cluster_id = '*'` fleet row. `inconclusive` increments neither (no signal).

### 2.3 Attribution — no new table

> **Status: deferred to a future increment (not in v1).** The `details` JSONB column
> ships (default `'{}'`), but v1 does not populate `likely_change` hints — there is no
> consumer wired to surface them yet (the Learning UI / MCP tool show verdicts + track
> record, not per-case attribution), so writing them now would be a half-feature. The
> design below is retained as the intended shape for when a consumer is added.

Change/approval context would be read at evaluation time from existing stores:

- `event_log` (cluster_id, event_time, event_type, source, message, raw_event) — schema
  changes, RDS events, alerts, anomalies, writes.
- Approval records (DynamoDB) — what change was approved + executed and when.

Any change in `[opened_at, evaluated_at]` would be attached to `remediation_cases.details`
as a `likely_change` hint. **Never asserted as the cause** (see §7).

---

## 3. Case lifecycle — opening

A thin enricher opens/refreshes cases at the existing emission points. No emission
point changes its own behavior; the case write is best-effort and never blocks it.

| Source                 | Trigger                                | symptom_class                  | watch_metric                               | action_class                                       |
| ---------------------- | -------------------------------------- | ------------------------------ | ------------------------------------------ | -------------------------------------------------- |
| `proactive_monitor`    | writes `event_log` `anomaly_<metric>`  | `anomaly:<metric>`             | `<metric>`                                 | inferred from recommendation, else `manual`        |
| finding collectors     | inserts `cluster_health_findings`      | `finding:<check_type>`         | metric if the check maps to one, else NULL | inferred from `recommendation` text → action_class |
| `task_worker._run_rca` | RCA completes with `recommendations[]` | `rca:<top_candidate.category>` | top candidate's metric if any              | inferred per recommendation                        |

**`action_class` inference** is a small deterministic classifier mapping
recommendation text / RCA category to a normalized action: keywords like
인덱스→`index_add`, work_mem·max_connections·파라미터→`param_change`, 스케일·ACU→
`scale_up`, VACUUM→`vacuum`, ANALYZE→`analyze`; default `manual`. One pure function,
unit-tested against the actual recommendation strings the collectors emit.

**Packaging note:** the enricher runs in two Lambda packages that cannot share imports
— `data-pipeline/` (proactive_monitor + ETL finding collectors) and
`mcp-servers/mcp_servers/workers/` (task_worker). The classifier is therefore a tiny
self-contained module **copied into both, kept in sync**, matching the existing
`ws_notify` / `_broadcast` copy pattern in this repo. Both copies share the same unit
test fixture so they can't silently diverge.

**Dedup:** the partial unique index makes re-emission idempotent — `INSERT … ON
CONFLICT (open case) DO UPDATE SET last_seen_at = NOW()`.

**`evaluate_after` = opened_at + window(symptom_class).** Window is a per-class tuning
knob (default 6h for metric symptoms, 24h for recurring findings — a finding only
re-runs each ETL so it needs a longer observation window to confirm clearance).

---

## 4. Outcome evaluator (the automatic verdict)

New scheduled Lambda `data-pipeline/outcome_evaluator/handler.py`, EventBridge every
20 min. Lives in the **data stack** (touches only RDS Data API + DynamoDB + event_log),
mirroring `proactive_monitor` / `alert_evaluator`.

For each case with `status='open' AND evaluate_after <= NOW()`:

### 4.1 Metric-backed cases (`watch_metric` set)

Reuse the seasonal baseline already trained by `pg_baseline_trainer`:

- Pull the case's `watch_metric` from `metric_snapshots` over the post-open window.
- Compare against `metric_baselines` for the current `(metric_type, hour_of_week)`
  bucket: in-band = `median ± k·IQR` (k default 3, same robust z-score the detector uses).
- **resolved** = back in band and held for the trailing eval window.
- **persisted** = still out of band.
- **inconclusive** = no recent snapshots / no baseline bucket yet.

### 4.2 Derived-finding cases (`watch_metric` NULL)

- **resolved** = the same finding `(check_type, subject)` was NOT regenerated in
  `cluster_health_findings` during the eval window.
- **persisted** = it was regenerated (still firing).

### 4.3 False-resolved guard (critical correctness)

A finding can vanish because the **collector stopped running**, not because the
problem cleared. Before declaring a finding-case `resolved`, confirm the collector
actually ran in the window: require that the cluster produced _some_
`cluster_health_findings` row **or** fresh `metric_snapshots` for that engine during
the window. If there's no evidence the collector ran → `inconclusive`, not resolved.

```
# ponytail: "finding disappeared" is only a success if the collector that emits it
# actually ran in the window. No collector heartbeat table — proxy on "did this
# cluster produce ANY finding/metric row in the window". Upgrade to a real
# per-collector heartbeat only if the proxy mislabels.
```

### 4.4 On verdict

- Set `status`, `evaluated_at`. (Appending `likely_change` hints to `details` is
  deferred — see §2.3; v1 leaves `details` at its `'{}'` default.)
- `resolved` → `attempts += 1, successes += 1` on both the cluster row and `'*'` row.
  `persisted` → `attempts += 1`. `inconclusive` → no agg change; optionally re-arm
  (push `evaluate_after` out once) so a slow signal gets a second look before giving up.

---

## 5. Consumers (feedback into recommendations)

### Phase 1 — deterministic re-rank + confidence badge (no LLM)

- Findings read path (`api/dashboard` / `api/alerts` findings endpoints) LEFT JOINs
  `remediation_outcomes_agg` on `(cluster_id, symptom_class, action_class)` with a
  fallback to the `'*'` fleet row when the cluster has no history.
- Each recommendation carries `{successes, attempts, confidence}`; the UI shows a badge
  ("이 클러스터에서 4/5회 해결" / fleet fallback "전체 3/4회"). Proven actions sort up,
  historically-failed actions sort down. Confidence is a simple smoothed rate
  (e.g. Wilson lower bound) so 1/1 doesn't outrank 9/10.

### Phase 2 — LLM prompt injection + agent tool

- `task_worker._narrative` fetches the relevant agg rows for the symptom and injects a
  compact line into the RCA prompt ("과거 효과 — index_add 4/5, param_change 1/3"),
  constrained so the model cites evidence rather than inventing it.
- New gateway MCP tool `get_remediation_history(cluster_id, symptom_class)` (incident
  server) so the live chat agent can pull the track record on demand. Read-only, added
  to `tool_definitions` + the gateway schema (parity test).

---

## 6. UI surface

- **Inline:** confidence badge + "효과 이력" on each finding/recommendation card
  (dashboard, alerts, RCA result).
- **Dedicated view:** a read-only "Learning" (효과 학습) page — per-cluster and fleet
  remediation track record (symptom → action → success rate), recent resolved/persisted
  cases with their attribution hints. This is the "the platform is learning" screen.
  - **Placement:** new top-level nav page `app/learning/page.tsx` (eyebrow "Monitor"),
    sibling to Tasks/Map. Reads a new `GET /api/learning` endpoint (agent stack,
    `add_routes`). Static export + Playwright smoke like the rest of the frontend.
  - i18n per repo rule: DBA jargon stays English; explanatory copy / empty states Korean.

---

## 7. Honesty & ceilings (explicit)

- **Attribution ≠ causation.** The verdict is _symptom resolution_, not "this action
  caused it." `likely_change` hints from event_log/approvals are correlational. The UI
  always shows the sample size N; a single resolution is not a claim, a track record is.
- **Cold start.** A new cluster has no history → fall back to the `'*'` fleet prior;
  show that it's a fleet-level number, not cluster-specific.
- **Evaluation window is a tuning knob** per `symptom_class`; too short → false
  "persisted", too long → slow learning. Defaults in §3, overridable via settings.
- **False-resolved guard** (§4.3) is the subtle correctness risk and is explicitly
  tested.
- **No write actions added.** This feature only _observes and ranks_. Applying changes
  still goes through the existing `approval_guard`; the loop never auto-applies anything.

---

## 8. Scope

**In (v1):** the three existing emit points — `proactive_monitor` anomalies,
`cluster_health_findings` (query_regression, pg_param_fitness, capacity_forecast,
pg_engine_internals, dynamodb/docdb/elasticache findings), and RCA task recommendations.

**Out (v1):** `recommend_index` and other on-demand performance tools — they don't
persist a finding today, so there's no durable emit point to anchor a case. They fold
in automatically if/when their output is persisted as a finding.

**Phasing:**

- **Phase 1** (closes the loop, demoable, deterministic): `schema_v24.sql` + case
  enricher at the 3 emit points + `outcome_evaluator` + agg store + findings
  re-rank/confidence badge + Learning UI (read-only).
- **Phase 2** (LLM memory): RCA/chat prompt injection + `get_remediation_history` tool.

---

## 9. Components & files

| Component                      | Location                                                                                    | Notes                                                       |
| ------------------------------ | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Schema                         | `data-pipeline/schema_migrator/sql/schema_v24.sql`                                          | 3 tables, §2                                                |
| action_class classifier        | `remediation_classify.py` copied into `data-pipeline/` + `mcp-servers/mcp_servers/workers/` | pure, kept in sync (ws_notify pattern), shared test fixture |
| Case enricher                  | hook in `proactive_monitor`, finding collectors, `task_worker`                              | best-effort, never blocks emit                              |
| Outcome evaluator              | `data-pipeline/outcome_evaluator/handler.py`                                                | EventBridge 20 min, data stack                              |
| Aggregate updates              | inside evaluator                                                                            | cluster row + `'*'` row                                     |
| Findings re-rank/badge         | `api/dashboard` + `api/alerts` findings read paths                                          | LEFT JOIN agg + Wilson rate                                 |
| Learning API                   | `GET /api/learning` (agent stack `add_routes`)                                              | per-cluster + fleet track record                            |
| Learning UI                    | `frontend/src/app/learning/page.tsx` + nav                                                  | static export, Playwright smoke                             |
| Prompt injection (P2)          | `task_worker._narrative` + live agent                                                       | constrained to agg rows                                     |
| `get_remediation_history` (P2) | incident MCP server                                                                         | gateway schema parity                                       |
| CDK                            | `data_stack.py` (evaluator Lambda + EventBridge + IAM), `agent_stack.py` (route + UI)       | CDK-only                                                    |

---

## 10. Testing

- **Unit (evaluator verdict):** metric in-band vs out-of-band against a seeded
  `metric_baselines` bucket; finding recurrence vs clearance; **false-resolved guard**
  (collector-didn't-run → inconclusive, not resolved); cold-start fleet fallback.
- **Unit:** case open + dedup (partial unique index), agg increment (both rows),
  action_class classifier against the real recommendation strings the collectors emit.
- **Integration:** against the Aurora PG cache with seeded `metric_snapshots`,
  `metric_baselines`, `cluster_health_findings`, and `remediation_cases`.
- **CDK snapshot** for the new Lambda + schedule + IAM.
- **Frontend:** Playwright smoke for the Learning page render.
- **Parity (P2):** `get_remediation_history` handler ↔ gateway schema.
