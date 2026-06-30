# Remediation Outcome Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the recommendation loop — when DBOps emits a recommendation, automatically judge whether the triggering symptom resolved, accumulate per-cluster/symptom/action success rates, and feed that evidence back into the findings UI (badge + re-rank) and the agent's narrative.

**Architecture:** A single scheduled Lambda (`outcome_evaluator`) does two things each run: (1) **pull-based case opener** — scans recent `cluster_health_findings` and `event_log` anomaly rows and opens a `remediation_cases` row per live symptom (deduped); (2) **verdict pass** — for cases past their evaluation window, judges `resolved`/`persisted`/`inconclusive` from seasonal-baseline recovery (metric symptoms) or finding-recurrence clearance (derived findings), then increments `remediation_outcomes_agg` (per-cluster + a `'*'` fleet rollup). Consumers read the aggregate: the findings API attaches a track-record badge and re-ranks; (phase 2) the RCA worker injects the history into its prompt and a new MCP tool exposes it to the chat agent.

**Why pull-based (deviation from spec's "enricher at emission points"):** opening cases by scanning the two tables the emitters already write (`cluster_health_findings`, `event_log`) lives in ONE place, touches none of the ~8 finding collectors or `proactive_monitor`, and satisfies the spec's "no emission point changes its own behavior." This supersedes the spec §3 "copied into both packages" packaging note — the `action_class` classifier now lives in exactly one package (`data-pipeline/outcome_evaluator/`).

**Tech Stack:** Python 3.12 Lambdas (RDS Data API via `boto3` `rds-data`), Aurora PostgreSQL Serverless v2 cache, AWS CDK (Python), Next.js 16 static export + TypeScript, pytest.

## Global Constraints

- **CDK-only infra.** All AWS resources via `cdk/stacks/*`; env values in `cdk/config/settings.py`. Never AWS CLI/Console.
- **Every cache SQL carries an audit comment** `/* source=dbops-outcome-eval */` (the `_query` helper prepends it).
- **RDS Data API reads need `includeResultMetadata=True`** or `columnMetadata` is missing and name-based rows become empty dicts.
- **Data-pipeline unit tests mock `execute_statement` / the module `_query`**, not the high-level helper (mirror `tests/unit/data_pipeline/test_task_scheduler.py`).
- **New API route ⇒ a dedicated `self.api.add_routes(...)` in `cdk/stacks/agent_stack.py`** (routes are registered per-path).
- **Frontend:** Next.js 16 **static export**; **no JS unit runner** — cover pure helpers with `tsc --noEmit` + a Playwright smoke. **prettier pre-commit reformats on first commit** → `git add -A` and re-commit (do not chain commit+push). DBA jargon stays English (Replica Lag, AAS…); explanatory copy / empty states in Korean. Numbers ≥ 1000 use `fmtDecimal`/`fmtExact`.
- **No Claude `Co-Authored-By` trailer in commits.** Use the repo commit-trailer protocol (Constraint/Rejected/Confidence/…) when non-trivial.
- **This feature observes and ranks only — it never auto-applies a change.** Writes still go through the existing `approval_guard`. Attribution is a hint, never a causal claim.
- **Phase 1 case sources = findings + anomalies** (both in PG). RCA-sourced cases are Phase 2 (they require an `agent-tasks` DynamoDB read). This is a deliberate scope line.

---

## File structure

**Phase 1**

- `data-pipeline/schema_migrator/sql/schema_v24.sql` — _create:_ 3 tables + indexes.
- `data-pipeline/outcome_evaluator/remediation_classify.py` — _create:_ pure `classify_action()`.
- `data-pipeline/outcome_evaluator/case_opener.py` — _create:_ `open_cases(query)`.
- `data-pipeline/outcome_evaluator/evaluator.py` — _create:_ `evaluate_case()`, `apply_verdict()`.
- `data-pipeline/outcome_evaluator/handler.py` — _create:_ `_query` helper + `lambda_handler`.
- `cdk/stacks/data_stack.py` — _modify:_ add `outcome_evaluator` Lambda + 20-min schedule + grants.
- `api/dashboard/handler.py` — _modify:_ `_health_findings` enrichment (+ `/api/learning` early branch).
- `cdk/stacks/agent_stack.py` — _modify:_ `add_routes("/api/learning")`.
- `frontend/src/lib/remediation.ts` — _create:_ pure confidence/format helpers.
- `frontend/src/lib/api-client.ts` — _modify:_ `fetchLearning()`.
- `frontend/src/app/learning/page.tsx` — _create:_ Learning page.
- `frontend/src/components/app-shell.tsx` — _modify:_ nav entry.
- `frontend/e2e/smoke.spec.ts` — _modify:_ Learning render assertion.

**Phase 2**

- `data-pipeline/outcome_evaluator/case_opener.py` — _modify:_ `open_rca_cases(query, ddb)`.
- `cdk/stacks/data_stack.py` — _modify:_ grant `agent-tasks` read to `outcome_evaluator`.
- `mcp-servers/mcp_servers/workers/task_worker.py` — _modify:_ inject history into `_narrative`.
- `mcp-servers/mcp_servers/incident/tools/remediation_history.py` — _create:_ MCP tool.
- `mcp-servers/mcp_servers/incident/handler.py` + gateway schema — _modify:_ register tool.

---

## Phase 1

### Task 1: Schema — `remediation_cases`, `remediation_outcomes_agg`

**Files:**

- Create: `data-pipeline/schema_migrator/sql/schema_v24.sql`
- Test: `tests/unit/data_pipeline/test_schema_v24.py`

**Interfaces:**

- Produces: tables `remediation_cases` (cols per code below), `remediation_outcomes_agg`; partial unique index `ux_remediation_cases_open`. The migrator (`schema_migrator/handler.py`) auto-applies `schema_v*.sql` in numeric order on data-stack deploy.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_schema_v24.py
import pathlib

SQL = (pathlib.Path(__file__).parents[3]
       / "data-pipeline/schema_migrator/sql/schema_v24.sql").read_text()

def test_declares_three_objects():
    assert "CREATE TABLE IF NOT EXISTS remediation_cases" in SQL
    assert "CREATE TABLE IF NOT EXISTS remediation_outcomes_agg" in SQL
    # Partial unique index is what makes "one open case per symptom" enforceable.
    assert "ux_remediation_cases_open" in SQL
    assert "WHERE status = 'open'" in SQL

def test_agg_primary_key_is_three_cols():
    assert "PRIMARY KEY (cluster_id, symptom_class, action_class)" in SQL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/data_pipeline/test_schema_v24.py -v`
Expected: FAIL — file does not exist (`FileNotFoundError`).

- [ ] **Step 3: Write the schema file**

```sql
-- data-pipeline/schema_migrator/sql/schema_v24.sql
-- Remediation Outcome Loop: cases + learned aggregate. See
-- docs/superpowers/specs/2026-06-30-remediation-outcome-loop-design.md

CREATE TABLE IF NOT EXISTS remediation_cases (
    case_id             BIGSERIAL PRIMARY KEY,
    cluster_id          VARCHAR(255) NOT NULL,
    opened_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    symptom_class       VARCHAR(80)  NOT NULL,
    symptom_subject     VARCHAR(255) NOT NULL DEFAULT '',
    watch_metric        VARCHAR(80),
    severity_at_open    VARCHAR(20),
    recommendation_text TEXT,
    action_class        VARCHAR(40)  NOT NULL DEFAULT 'manual',
    source              VARCHAR(40)  NOT NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'open',
    evaluate_after      TIMESTAMPTZ  NOT NULL,
    evaluated_at        TIMESTAMPTZ,
    details             JSONB        NOT NULL DEFAULT '{}'::jsonb
);

-- At most one OPEN case per (cluster, symptom_class, subject); re-emission while
-- open only bumps last_seen_at (see case_opener ON CONFLICT).
CREATE UNIQUE INDEX IF NOT EXISTS ux_remediation_cases_open
    ON remediation_cases (cluster_id, symptom_class, symptom_subject)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS ix_remediation_cases_due
    ON remediation_cases (status, evaluate_after);

CREATE TABLE IF NOT EXISTS remediation_outcomes_agg (
    cluster_id      VARCHAR(255) NOT NULL,   -- '*' = fleet rollup (cold-start prior)
    symptom_class   VARCHAR(80)  NOT NULL,
    action_class    VARCHAR(40)  NOT NULL,
    attempts        INTEGER      NOT NULL DEFAULT 0,
    successes       INTEGER      NOT NULL DEFAULT 0,
    last_outcome    VARCHAR(20),
    last_success_at TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, symptom_class, action_class)
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/data_pipeline/test_schema_v24.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/schema_migrator/sql/schema_v24.sql tests/unit/data_pipeline/test_schema_v24.py
git commit -m "feat(outcome-loop): schema_v24 — remediation_cases + outcomes_agg"
```

---

### Task 2: `action_class` classifier (pure)

**Files:**

- Create: `data-pipeline/outcome_evaluator/__init__.py` (empty), `data-pipeline/outcome_evaluator/remediation_classify.py`
- Test: `tests/unit/data_pipeline/test_remediation_classify.py`

**Interfaces:**

- Produces: `classify_action(text: str, category: str = "") -> str` → one of `index_add | param_change | scale_up | vacuum | analyze | manual`. Used by `case_opener` (Task 3) and `open_rca_cases` (Task 10).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_remediation_classify.py
from outcome_evaluator.remediation_classify import classify_action

def test_index_and_param_and_scale():
    assert classify_action("Query Lab에서 EXPLAIN으로 플랜 확인, 필요하면 인덱스 점검") == "index_add"
    assert classify_action("work_mem 및 max_connections 파라미터를 조정하세요") == "param_change"
    assert classify_action("ACU 상한을 올려 스케일 업하세요") == "scale_up"

def test_vacuum_analyze_and_default():
    assert classify_action("autovacuum/VACUUM 점검 권장") == "vacuum"
    assert classify_action("통계가 오래됨 — ANALYZE 실행") == "analyze"
    assert classify_action("원인 불명, 수동 점검 필요") == "manual"
    assert classify_action("") == "manual"
```

(Run via `conftest`/`pyproject` that puts `data-pipeline/` on `sys.path`; the existing
data_pipeline tests already import bare module names like `task_scheduler`, so the same
path config applies — confirm by running an existing test first.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/data_pipeline/test_remediation_classify.py -v`
Expected: FAIL — `ModuleNotFoundError: outcome_evaluator`.

- [ ] **Step 3: Implement**

```python
# data-pipeline/outcome_evaluator/remediation_classify.py
"""Map a free-text recommendation (or RCA category) to a normalized action_class.

Pure + deterministic so the same (symptom, action) key is produced wherever a case
is opened. Order matters: the FIRST matching family wins, most-specific first.
"""

# (substring, action_class) — checked in order; Korean + English keywords.
_RULES = [
    ("인덱스", "index_add"), ("index", "index_add"),
    ("vacuum", "vacuum"), ("배큠", "vacuum"), ("autovacuum", "vacuum"),
    ("analyze", "analyze"), ("통계", "analyze"),
    ("work_mem", "param_change"), ("max_connection", "param_change"),
    ("파라미터", "param_change"), ("parameter", "param_change"),
    ("스케일", "scale_up"), ("scal", "scale_up"), ("acu", "scale_up"),
]


def classify_action(text: str, category: str = "") -> str:
    hay = f"{text or ''} {category or ''}".lower()
    for needle, action in _RULES:
        if needle in hay:
            return action
    return "manual"
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/data_pipeline/test_remediation_classify.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/outcome_evaluator/__init__.py data-pipeline/outcome_evaluator/remediation_classify.py tests/unit/data_pipeline/test_remediation_classify.py
git commit -m "feat(outcome-loop): deterministic action_class classifier"
```

---

### Task 3: Case opener (findings + anomalies → open cases)

**Files:**

- Create: `data-pipeline/outcome_evaluator/case_opener.py`
- Test: `tests/unit/data_pipeline/test_case_opener.py`

**Interfaces:**

- Consumes: `classify_action` (Task 2); a `query(sql, params=None) -> list[dict]` callable (provided by the handler, Task 5).
- Produces: `open_cases(query) -> int` (count opened/refreshed). Writes `remediation_cases` via INSERT … ON CONFLICT.

Window constants: metric symptoms re-evaluate after **6h**, recurring findings after **24h** (a finding only re-runs each ETL, so it needs a longer observation window to confirm clearance).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_case_opener.py
from outcome_evaluator import case_opener

def _capturing_query(finding_rows, anomaly_rows):
    """Returns a query() stub that answers the two SELECTs by SQL keyword and
    records every INSERT it receives."""
    inserts = []
    def query(sql, params=None):
        if "cluster_health_findings" in sql and "SELECT" in sql:
            return finding_rows
        if "event_log" in sql and "SELECT" in sql:
            return anomaly_rows
        if sql.strip().upper().startswith("INSERT INTO REMEDIATION_CASES"):
            inserts.append(params)
            return []
        return []
    query.inserts = inserts
    return query

def test_opens_finding_and_anomaly_cases():
    q = _capturing_query(
        finding_rows=[{"cluster_id": "c1", "check_type": "query_regression",
                       "subject": "SELECT ...", "severity": "warning",
                       "recommendation": "인덱스 점검", "snapshot_time": "2026-06-30T00:00:00Z"}],
        anomaly_rows=[{"cluster_id": "c1", "event_type": "anomaly_cpu",
                       "message": "...", "event_time": "2026-06-30T00:01:00Z"}],
    )
    n = case_opener.open_cases(q)
    assert n == 2
    by_class = {p["symptom_class"]: p for p in q.inserts}
    assert by_class["finding:query_regression"]["action_class"] == "index_add"
    assert by_class["finding:query_regression"]["watch_metric"] is None
    assert by_class["anomaly:cpu"]["watch_metric"] == "cpu"
    assert by_class["anomaly:cpu"]["symptom_subject"] == "cpu"

def test_no_rows_opens_nothing():
    q = _capturing_query([], [])
    assert case_opener.open_cases(q) == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/data_pipeline/test_case_opener.py -v`
Expected: FAIL — `ModuleNotFoundError: outcome_evaluator.case_opener`.

- [ ] **Step 3: Implement**

```python
# data-pipeline/outcome_evaluator/case_opener.py
"""Pull-based case opener. Scans the two tables emitters already write
(cluster_health_findings, event_log anomalies) and opens one remediation_cases
row per live symptom. Idempotent via the partial unique index — re-emission while
a case is open only bumps last_seen_at.
"""
from outcome_evaluator.remediation_classify import classify_action

# How far back to scan each run. A little wider than the evaluator cadence so
# nothing slips between runs; ON CONFLICT makes overlap harmless.
SCAN_WINDOW = "INTERVAL '1 hour'"
WIN_METRIC_MIN = 360    # 6h  — metric-symptom cases
WIN_FINDING_MIN = 1440  # 24h — recurring-finding cases

_INSERT = (
    "INSERT INTO remediation_cases "
    "(cluster_id, symptom_class, symptom_subject, watch_metric, severity_at_open, "
    " recommendation_text, action_class, source, evaluate_after) "
    "VALUES (:cluster_id, :symptom_class, :symptom_subject, :watch_metric, :severity_at_open, "
    " :recommendation_text, :action_class, :source, "
    " NOW() + (:win_min || ' minutes')::interval) "
    "ON CONFLICT (cluster_id, symptom_class, symptom_subject) WHERE status = 'open' "
    "DO UPDATE SET last_seen_at = NOW()"
)


def open_cases(query) -> int:
    opened = 0

    findings = query(
        "SELECT cluster_id, check_type, subject, severity, recommendation "
        "FROM cluster_health_findings "
        f"WHERE snapshot_time > NOW() - {SCAN_WINDOW}"
    )
    for f in findings or []:
        query(_INSERT, {
            "cluster_id": f["cluster_id"],
            "symptom_class": f"finding:{f['check_type']}",
            "symptom_subject": (f.get("subject") or "")[:255],
            "watch_metric": None,  # findings are judged by recurrence, not a metric
            "severity_at_open": f.get("severity"),
            "recommendation_text": f.get("recommendation"),
            "action_class": classify_action(f.get("recommendation") or ""),
            "source": "finding_collector",
            "win_min": WIN_FINDING_MIN,
        })
        opened += 1

    anomalies = query(
        "SELECT cluster_id, event_type, message "
        "FROM event_log "
        f"WHERE event_type LIKE 'anomaly_%' AND event_time > NOW() - {SCAN_WINDOW}"
    )
    for a in anomalies or []:
        metric = (a["event_type"] or "")[len("anomaly_"):]  # 'anomaly_cpu' -> 'cpu'
        query(_INSERT, {
            "cluster_id": a["cluster_id"],
            "symptom_class": f"anomaly:{metric}",
            "symptom_subject": metric,
            "watch_metric": metric,  # judged by baseline recovery
            "severity_at_open": None,
            "recommendation_text": a.get("message"),
            # anomaly alerts carry no prescribed action; 'manual' = "resolved on its own / unspecified"
            "action_class": "manual",
            "source": "proactive_monitor",
            "win_min": WIN_METRIC_MIN,
        })
        opened += 1

    return opened
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/data_pipeline/test_case_opener.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/outcome_evaluator/case_opener.py tests/unit/data_pipeline/test_case_opener.py
git commit -m "feat(outcome-loop): pull-based case opener (findings + anomalies)"
```

---

### Task 4: Verdict evaluator + aggregate update

**Files:**

- Create: `data-pipeline/outcome_evaluator/evaluator.py`
- Test: `tests/unit/data_pipeline/test_evaluator.py`

**Interfaces:**

- Consumes: a `query(sql, params=None) -> list[dict]` callable.
- Produces:
  - `evaluate_case(query, case: dict) -> str` → `'resolved' | 'persisted' | 'inconclusive'`.
  - `apply_verdict(query, case: dict, verdict: str) -> None` — updates the case row and (on resolved/persisted) increments `remediation_outcomes_agg` for both `case['cluster_id']` and `'*'`.
- `case` dict keys used: `case_id, cluster_id, symptom_class, symptom_subject, watch_metric, action_class, opened_at`.

Verdict rules:

- **Metric case** (`watch_metric` set): recent avg of the metric vs its `metric_baselines` bucket (`median ± K·IQR`, K=3). In band ⇒ `resolved`; out ⇒ `persisted`; no recent data or no baseline bucket ⇒ `inconclusive`.
- **Finding case** (`watch_metric` NULL): did the same `(check_type, subject)` recur since `opened_at`? Recurred ⇒ `persisted`. Not recurred — but ONLY trust that if the collector actually ran in the window (**false-resolved guard**: the cluster produced _some_ finding row since `opened_at`); else ⇒ `inconclusive`. Cleared + collector ran ⇒ `resolved`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_evaluator.py
from outcome_evaluator import evaluator

def _query_router(routes):
    """routes: list of (sql_substring, rows). First substring match answers."""
    def query(sql, params=None):
        for sub, rows in routes:
            if sub in sql:
                return rows
        return []
    return query

def test_metric_case_resolved_when_back_in_band():
    q = _query_router([
        ("AVG(value)", [{"v": 30.0}]),                       # recent avg
        ("metric_baselines", [{"median": 28.0, "iqr": 5.0}]),# 28 ± 3*5 = [13,43] → 30 in band
    ])
    case = {"cluster_id": "c1", "symptom_class": "anomaly:cpu", "symptom_subject": "cpu",
            "watch_metric": "cpu", "action_class": "manual", "opened_at": "2026-06-30T00:00:00Z"}
    assert evaluator.evaluate_case(q, case) == "resolved"

def test_metric_case_persisted_when_out_of_band():
    q = _query_router([
        ("AVG(value)", [{"v": 95.0}]),
        ("metric_baselines", [{"median": 28.0, "iqr": 5.0}]),
    ])
    case = {"cluster_id": "c1", "symptom_class": "anomaly:cpu", "symptom_subject": "cpu",
            "watch_metric": "cpu", "action_class": "manual", "opened_at": "x"}
    assert evaluator.evaluate_case(q, case) == "persisted"

def test_metric_case_inconclusive_without_baseline():
    q = _query_router([("AVG(value)", [{"v": 30.0}]), ("metric_baselines", [])])
    case = {"watch_metric": "cpu", "symptom_subject": "cpu", "cluster_id": "c1",
            "symptom_class": "anomaly:cpu", "action_class": "manual", "opened_at": "x"}
    assert evaluator.evaluate_case(q, case) == "inconclusive"

def test_finding_case_resolved_only_if_collector_ran():
    # not recurred (0) + collector ran (other findings exist → cnt>0) ⇒ resolved
    q = _query_router([
        ("AS recurred", [{"recurred": 0}]),
        ("AS produced", [{"produced": 4}]),
    ])
    case = {"cluster_id": "c1", "symptom_class": "finding:query_regression",
            "symptom_subject": "SELECT ...", "watch_metric": None,
            "action_class": "index_add", "opened_at": "x"}
    assert evaluator.evaluate_case(q, case) == "resolved"

def test_finding_case_inconclusive_when_collector_silent():
    # not recurred (0) BUT collector produced nothing → can't call it resolved
    q = _query_router([("AS recurred", [{"recurred": 0}]), ("AS produced", [{"produced": 0}])])
    case = {"cluster_id": "c1", "symptom_class": "finding:query_regression",
            "symptom_subject": "s", "watch_metric": None, "action_class": "index_add", "opened_at": "x"}
    assert evaluator.evaluate_case(q, case) == "inconclusive"

def test_apply_verdict_resolved_bumps_both_agg_rows():
    seen = []
    def query(sql, params=None):
        seen.append((sql, params)); return []
    case = {"case_id": 7, "cluster_id": "c1", "symptom_class": "anomaly:cpu", "action_class": "manual"}
    evaluator.apply_verdict(query, case, "resolved")
    agg = [p for s, p in seen if "remediation_outcomes_agg" in s]
    assert {p["cluster_id"] for p in agg} == {"c1", "*"}
    assert all(p["succ_inc"] == 1 for p in agg)  # resolved ⇒ successes += 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/data_pipeline/test_evaluator.py -v`
Expected: FAIL — `ModuleNotFoundError: outcome_evaluator.evaluator`.

- [ ] **Step 3: Implement**

```python
# data-pipeline/outcome_evaluator/evaluator.py
"""Judge open cases and fold the verdict into the learned aggregate."""

K = 3  # robust band half-width in IQRs (matches the anomaly detector's z>3)
EVAL_LOOKBACK_MIN = 60  # recent-window for metric recovery


def _first(rows, key, default=None):
    return rows[0].get(key) if rows else default


def evaluate_case(query, case) -> str:
    if case.get("watch_metric"):
        return _evaluate_metric(query, case)
    return _evaluate_finding(query, case)


def _evaluate_metric(query, case) -> str:
    recent = query(
        "SELECT AVG(value) AS v FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = :m "
        f"AND ts > NOW() - INTERVAL '{EVAL_LOOKBACK_MIN} minutes' "
        "AND (dimensions IS NULL OR NOT jsonb_exists(dimensions, 'instance'))",
        {"cid": case["cluster_id"], "m": case["watch_metric"]},
    )
    v = _first(recent, "v")
    if v is None:
        return "inconclusive"
    base = query(
        "SELECT median, iqr FROM metric_baselines "
        "WHERE cluster_id = :cid AND metric_type = :m "
        "AND hour_of_week = (EXTRACT(DOW FROM NOW())::int * 24 + EXTRACT(HOUR FROM NOW())::int)",
        {"cid": case["cluster_id"], "m": case["watch_metric"]},
    )
    if not base or _first(base, "median") is None:
        return "inconclusive"
    median, iqr = float(base[0]["median"]), float(base[0]["iqr"])
    lo, hi = median - K * iqr, median + K * iqr
    return "resolved" if lo <= float(v) <= hi else "persisted"


def _evaluate_finding(query, case) -> str:
    recurred = query(
        "SELECT COUNT(*) AS recurred FROM cluster_health_findings "
        "WHERE cluster_id = :cid AND check_type = :ct AND subject = :subj "
        "AND snapshot_time > :since",
        {"cid": case["cluster_id"], "ct": case["symptom_class"].split(":", 1)[1],
         "subj": case["symptom_subject"], "since": case["opened_at"]},
    )
    if int(_first(recurred, "recurred", 0) or 0) > 0:
        return "persisted"
    # False-resolved guard: only trust "cleared" if the collector actually ran —
    # i.e. the cluster produced ANY finding row since the case opened.
    produced = query(
        "SELECT COUNT(*) AS produced FROM cluster_health_findings "
        "WHERE cluster_id = :cid AND snapshot_time > :since",
        {"cid": case["cluster_id"], "since": case["opened_at"]},
    )
    return "resolved" if int(_first(produced, "produced", 0) or 0) > 0 else "inconclusive"


def apply_verdict(query, case, verdict) -> None:
    query(
        "UPDATE remediation_cases SET status = :st, evaluated_at = NOW() WHERE case_id = :id",
        {"st": verdict, "id": case["case_id"]},
    )
    if verdict == "inconclusive":
        return  # no signal — don't move the aggregate
    succ_inc = 1 if verdict == "resolved" else 0
    for cid in (case["cluster_id"], "*"):
        query(
            "INSERT INTO remediation_outcomes_agg "
            "(cluster_id, symptom_class, action_class, attempts, successes, last_outcome, "
            " last_success_at, updated_at) "
            "VALUES (:cluster_id, :symptom_class, :action_class, 1, :succ_inc, :verdict, "
            " CASE WHEN :succ_inc = 1 THEN NOW() ELSE NULL END, NOW()) "
            "ON CONFLICT (cluster_id, symptom_class, action_class) DO UPDATE SET "
            " attempts = remediation_outcomes_agg.attempts + 1, "
            " successes = remediation_outcomes_agg.successes + :succ_inc, "
            " last_outcome = :verdict, "
            " last_success_at = CASE WHEN :succ_inc = 1 THEN NOW() "
            "                        ELSE remediation_outcomes_agg.last_success_at END, "
            " updated_at = NOW()",
            {"cluster_id": cid, "symptom_class": case["symptom_class"],
             "action_class": case["action_class"], "succ_inc": succ_inc, "verdict": verdict},
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/data_pipeline/test_evaluator.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add data-pipeline/outcome_evaluator/evaluator.py tests/unit/data_pipeline/test_evaluator.py
git commit -m "feat(outcome-loop): verdict evaluator + aggregate update (false-resolved guard)"
```

---

### Task 5: Lambda handler (`_query` + orchestration)

**Files:**

- Create: `data-pipeline/outcome_evaluator/handler.py`
- Test: `tests/unit/data_pipeline/test_outcome_handler.py`

**Interfaces:**

- Consumes: `case_opener.open_cases`, `evaluator.evaluate_case`, `evaluator.apply_verdict`.
- Produces: `lambda_handler(event, context) -> dict` `{opened, evaluated}`; module-level `_query(rds_data, ..., sql, params)` mirroring `proactive_monitor`/`task_scheduler` (includes `includeResultMetadata=True`, prepends `/* source=dbops-outcome-eval */`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/data_pipeline/test_outcome_handler.py
from unittest.mock import patch
from outcome_evaluator import handler

def test_opens_then_evaluates_due_cases(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:cluster")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:secret")
    due = [{"case_id": 1, "cluster_id": "c1", "symptom_class": "anomaly:cpu",
            "symptom_subject": "cpu", "watch_metric": "cpu", "action_class": "manual",
            "opened_at": "x"}]
    with patch.object(handler, "_query", return_value=None) as mq, \
         patch.object(handler.case_opener, "open_cases", return_value=3) as mo, \
         patch.object(handler, "_due_cases", return_value=due), \
         patch.object(handler.evaluator, "evaluate_case", return_value="resolved") as me, \
         patch.object(handler.evaluator, "apply_verdict") as ma:
        out = handler.lambda_handler({}, None)
    assert out == {"opened": 3, "evaluated": 1}
    mo.assert_called_once()
    me.assert_called_once()
    ma.assert_called_once()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/data_pipeline/test_outcome_handler.py -v`
Expected: FAIL — `ModuleNotFoundError: outcome_evaluator.handler`.

- [ ] **Step 3: Implement** (copy the `_query` body from `data-pipeline/proactive_monitor/handler.py` verbatim, changing only the audit comment)

```python
# data-pipeline/outcome_evaluator/handler.py
"""outcome_evaluator — open remediation cases, then judge the due ones.

EventBridge every 20 min. Public endpoints only (RDS Data API), so it lives in the
data stack like proactive_monitor / alert_evaluator.
"""
import os

import boto3

from outcome_evaluator import case_opener, evaluator


def _query(rds_data, cluster_arn, secret_arn, database, sql, params=None):
    sql_params = []
    if params:
        for k, v in params.items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            elif v is None:
                sql_params.append({"name": k, "value": {"isNull": True}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn, secretArn=secret_arn, database=database,
        sql=f"/* source=dbops-outcome-eval */ {sql}", parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [c["name"] for c in resp.get("columnMetadata", [])]
    rows = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
            else:
                row[col] = None
        rows.append(row)
    return rows


def _due_cases(q):
    return q(
        "SELECT case_id, cluster_id, symptom_class, symptom_subject, watch_metric, "
        "action_class, opened_at FROM remediation_cases "
        "WHERE status = 'open' AND evaluate_after <= NOW() LIMIT 500"
    )


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    def q(sql, params=None):
        return _query(rds_data, cluster_arn, secret_arn, database, sql, params)

    opened = case_opener.open_cases(q)
    evaluated = 0
    for case in _due_cases(q) or []:
        try:
            verdict = evaluator.evaluate_case(q, case)
            evaluator.apply_verdict(q, case, verdict)
            evaluated += 1
        except Exception as e:
            print(f"[outcome-eval] case {case.get('case_id')} failed: {type(e).__name__}: {e}")

    print(f"[outcome-eval] opened={opened} evaluated={evaluated}")
    return {"opened": opened, "evaluated": evaluated}
```

Note: the `isNull` param branch is required — `case_opener` passes `watch_metric=None`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/data_pipeline/test_outcome_handler.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Run the whole outcome-loop unit set + commit**

Run: `python -m pytest tests/unit/data_pipeline/test_schema_v24.py tests/unit/data_pipeline/test_remediation_classify.py tests/unit/data_pipeline/test_case_opener.py tests/unit/data_pipeline/test_evaluator.py tests/unit/data_pipeline/test_outcome_handler.py -v`
Expected: all PASS.

```bash
git add data-pipeline/outcome_evaluator/handler.py tests/unit/data_pipeline/test_outcome_handler.py
git commit -m "feat(outcome-loop): evaluator Lambda handler (open + judge due cases)"
```

---

### Task 6: CDK — wire `outcome_evaluator` Lambda + schedule

**Files:**

- Modify: `cdk/stacks/data_stack.py` (after the `task_scheduler` block, ~line 416)
- Test: `tests/cdk/test_synth.py` (run existing synth test — it must still pass)

**Interfaces:**

- Consumes: `self.cache_db`, `Settings`, the `lambda_/events/targets` imports already at the top of `data_stack.py`.
- Produces: `self.outcome_evaluator` Lambda on a 20-min `events.Rule`.

- [ ] **Step 1: Add the construct** (mirror the `TaskScheduler` block exactly)

```python
        # Remediation Outcome Loop — opens a case per emitted recommendation and
        # judges whether the symptom resolved (baseline recovery / finding
        # clearance), feeding remediation_outcomes_agg. Public endpoints only.
        self.outcome_evaluator = lambda_.Function(
            self, "OutcomeEvaluator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/outcome_evaluator"),
            timeout=cdk.Duration.seconds(120),
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        self.cache_db.secret.grant_read(self.outcome_evaluator)
        self.cache_db.grant_data_api_access(self.outcome_evaluator)
        events.Rule(
            self, "OutcomeEvaluatorSchedule",
            schedule=events.Schedule.rate(cdk.Duration.minutes(20)),
            targets=[targets.LambdaFunction(self.outcome_evaluator)],
        )
```

- [ ] **Step 2: Run synth test (regenerates/validates the template)**

Run: `python -m pytest tests/cdk/test_synth.py -v`
Expected: PASS. If the test snapshots templates and fails on the new resources, update the snapshot per the test's documented refresh command, then re-run to PASS.

- [ ] **Step 3: Verify the asset bundles the package** (the `__init__.py` from Task 2 makes `outcome_evaluator` importable inside the Lambda)

Run: `python -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('data-pipeline/outcome_evaluator/*.py')]; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add cdk/stacks/data_stack.py tests/cdk/test_synth.py
git commit -m "feat(outcome-loop): deploy outcome_evaluator Lambda on 20-min schedule"
```

---

### Task 7: Findings API enrichment (badge + re-rank)

**Files:**

- Modify: `api/dashboard/handler.py` — `_health_findings(query, cluster_id)` (~line 1818)
- Test: `tests/unit/api/test_health_findings_outcomes.py`

**Interfaces:**

- Consumes: the existing `query(sql, params)` callable inside the dashboard handler; tables `cluster_health_findings`, `remediation_outcomes_agg`.
- Produces: each finding dict gains `outcome: {successes, attempts}` (cluster row, falling back to the `'*'` fleet row); findings re-ranked by `(severity, success_rate)`. **No `action_class` needed at read time** — aggregate per `symptom_class = 'finding:'||check_type` across actions.

Read `_health_findings` first to get its exact current body, then make the change below.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/test_health_findings_outcomes.py
import importlib
dash = importlib.import_module("dashboard.handler")  # adjust to how api tests import it

def _query_stub():
    def query(sql, params=None):
        if "MAX(snapshot_time)" in sql:
            return [{"ts": "2026-06-30T00:00:00Z"}]
        if "FROM cluster_health_findings" in sql and "latest" in sql:
            return [{"id": 1, "check_type": "query_regression", "severity": "warning",
                     "subject": "s", "value_str": "", "threshold_str": "",
                     "recommendation": "인덱스", "details": "{}", "snapshot_time": "t"}]
        if "remediation_outcomes_agg" in sql:
            return [{"successes": 4, "attempts": 5}]
        return []
    return query

def test_finding_carries_outcome_track_record():
    out = dash._health_findings(_query_stub(), "c1")
    f = out["findings"][0]
    assert f["outcome"] == {"successes": 4, "attempts": 5}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/api/test_health_findings_outcomes.py -v`
Expected: FAIL — `KeyError: 'outcome'`.

- [ ] **Step 3: Implement** — after the findings `rows` are fetched in `_health_findings`, attach the track record and re-rank. Add before the `return`:

```python
    # Remediation Outcome Loop: attach each finding's track record (this cluster,
    # falling back to the '*' fleet rollup) and re-rank proven actions up.
    SEV_RANK = {"critical": 2, "warning": 1, "info": 0}
    for f in rows:
        sclass = f"finding:{f.get('check_type')}"
        agg = query(
            "SELECT COALESCE(SUM(successes),0) AS successes, COALESCE(SUM(attempts),0) AS attempts "
            "FROM remediation_outcomes_agg WHERE cluster_id = :cid AND symptom_class = :sc",
            {"cid": cluster_id, "sc": sclass},
        )
        s, a = (int(agg[0]["successes"]), int(agg[0]["attempts"])) if agg else (0, 0)
        if a == 0:  # cold start → fleet prior
            fleet = query(
                "SELECT COALESCE(SUM(successes),0) AS successes, COALESCE(SUM(attempts),0) AS attempts "
                "FROM remediation_outcomes_agg WHERE cluster_id = '*' AND symptom_class = :sc",
                {"sc": sclass},
            )
            s, a = (int(fleet[0]["successes"]), int(fleet[0]["attempts"])) if fleet else (0, 0)
        f["outcome"] = {"successes": s, "attempts": a}
    rows.sort(key=lambda f: (SEV_RANK.get(f.get("severity"), 0),
                             f["outcome"]["successes"] / (f["outcome"]["attempts"] + 1)),
              reverse=True)
```

(`rows` is the list already assigned to `"findings"` in the return dict; sorting it in place reorders the response.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/api/test_health_findings_outcomes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/dashboard/handler.py tests/unit/api/test_health_findings_outcomes.py
git commit -m "feat(outcome-loop): findings carry outcome track record + re-rank"
```

---

### Task 8: Learning API endpoint (`/api/learning`)

**Files:**

- Modify: `api/dashboard/handler.py` — add a `/api/learning` early branch in `lambda_handler` (near the `/multi-cluster/overview` branch, ~line 3446)
- Modify: `cdk/stacks/agent_stack.py` — `add_routes("/api/learning")` (near the other dashboard routes, ~line 1274)
- Test: `tests/unit/api/test_learning_endpoint.py`

**Interfaces:**

- Produces: `GET /api/learning` → `{"fleet": [...], "clusters": {cid: [...]}, "recent": [...]}` where each agg item is `{cluster_id, symptom_class, action_class, successes, attempts, last_outcome}` and `recent` is the last N resolved/persisted cases `{cluster_id, symptom_class, action_class, status, evaluated_at}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/test_learning_endpoint.py
import importlib
dash = importlib.import_module("dashboard.handler")

def test_learning_overview_groups_fleet_and_clusters():
    def query(sql, params=None):
        if "remediation_outcomes_agg" in sql:
            return [
                {"cluster_id": "*", "symptom_class": "anomaly:cpu", "action_class": "manual",
                 "successes": 3, "attempts": 4, "last_outcome": "resolved"},
                {"cluster_id": "c1", "symptom_class": "finding:query_regression",
                 "action_class": "index_add", "successes": 2, "attempts": 2, "last_outcome": "resolved"},
            ]
        if "FROM remediation_cases" in sql:
            return [{"cluster_id": "c1", "symptom_class": "finding:query_regression",
                     "action_class": "index_add", "status": "resolved", "evaluated_at": "t"}]
        return []
    body = dash._learning_overview(query)
    assert len(body["fleet"]) == 1 and body["fleet"][0]["symptom_class"] == "anomaly:cpu"
    assert "c1" in body["clusters"]
    assert body["recent"][0]["status"] == "resolved"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/api/test_learning_endpoint.py -v`
Expected: FAIL — `AttributeError: _learning_overview`.

- [ ] **Step 3: Implement the helper + wire the route branch**

```python
# api/dashboard/handler.py  — new helper
def _learning_overview(query):
    rows = query(
        "SELECT cluster_id, symptom_class, action_class, successes, attempts, last_outcome "
        "FROM remediation_outcomes_agg ORDER BY attempts DESC LIMIT 500"
    ) or []
    fleet, clusters = [], {}
    for r in rows:
        if r["cluster_id"] == "*":
            fleet.append(r)
        else:
            clusters.setdefault(r["cluster_id"], []).append(r)
    recent = query(
        "SELECT cluster_id, symptom_class, action_class, status, evaluated_at "
        "FROM remediation_cases WHERE status IN ('resolved','persisted') "
        "ORDER BY evaluated_at DESC LIMIT 50"
    ) or []
    return {"fleet": fleet, "clusters": clusters, "recent": recent}
```

Add the early branch in `lambda_handler` (mirror the `/multi-cluster/overview` branch — same auth/CORS path it uses):

```python
    if raw_path_early.endswith("/api/learning"):
        return _response(200, _learning_overview(query), max_age=30)
```

(Use the same `query` construction + `_response` helper the sibling `/multi-cluster/overview` branch uses; read that branch first and copy its setup.)

Add the route in `agent_stack.py`:

```python
        self.api.add_routes(
            path="/api/learning",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("LearningIntegration", dashboard_alias),
        )
```

- [ ] **Step 4: Run to verify it passes + synth still green**

Run: `python -m pytest tests/unit/api/test_learning_endpoint.py tests/cdk/test_synth.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/dashboard/handler.py cdk/stacks/agent_stack.py tests/unit/api/test_learning_endpoint.py
git commit -m "feat(outcome-loop): /api/learning overview endpoint + route"
```

---

### Task 9: Learning UI page + nav + confidence helper

**Files:**

- Create: `frontend/src/lib/remediation.ts`
- Modify: `frontend/src/lib/api-client.ts` (add `fetchLearning`)
- Create: `frontend/src/app/learning/page.tsx`
- Modify: `frontend/src/components/app-shell.tsx` (nav entry, mirror the "Map" entry)
- Modify: `frontend/e2e/smoke.spec.ts` (render assertion)

**Interfaces:**

- Consumes: `GET /api/learning` (Task 8) via `api-client`.
- Produces: `confidence(successes, attempts): number` (Wilson lower bound, 0..1) and `trackRecordLabel(successes, attempts): string` in `remediation.ts`; a `/learning` page.

- [ ] **Step 1: Write the pure helper** (`tsc` is the gate — no JS unit runner)

```ts
// frontend/src/lib/remediation.ts
// Wilson score lower bound at 95% — so 1/1 doesn't outrank 9/10. Pure + testable.
export function confidence(successes: number, attempts: number): number {
  if (attempts <= 0) return 0;
  const z = 1.96;
  const p = successes / attempts;
  const denom = 1 + (z * z) / attempts;
  const centre = p + (z * z) / (2 * attempts);
  const margin =
    z * Math.sqrt((p * (1 - p) + (z * z) / (4 * attempts)) / attempts);
  return Math.max(0, (centre - margin) / denom);
}

export function trackRecordLabel(successes: number, attempts: number): string {
  if (attempts <= 0) return "이력 없음";
  return `${successes}/${attempts}회 해결`;
}
```

- [ ] **Step 2: Add the API client method** (mirror an existing `fetch*` in `api-client.ts`, including auth header usage)

```ts
// frontend/src/lib/api-client.ts  (add near the other fetchers)
export async function fetchLearning(): Promise<{
  fleet: AggRow[];
  clusters: Record<string, AggRow[]>;
  recent: RecentCase[];
}> {
  return authedFetch(`${API_BASE}/api/learning`).then((r) => r.json());
}
// types AggRow / RecentCase: define alongside (cluster_id, symptom_class, action_class,
// successes, attempts, last_outcome / status, evaluated_at). Mirror the existing
// authedFetch + API_BASE usage in this file — read it first.
```

- [ ] **Step 3: Build the page** (mirror `frontend/src/app/map/page.tsx` shell: `PageHeader`/`PageBody`/`EmptyState`, loading + error states). Render: fleet track record, then per-cluster, then a "recent outcomes" list. Korean explanatory copy, English jargon, `fmtDecimal` for any count ≥ 1000.

```tsx
// frontend/src/app/learning/page.tsx
"use client";
import { useEffect, useState } from "react";
import { fetchLearning } from "@/lib/api-client";
import { confidence, trackRecordLabel } from "@/lib/remediation";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";

export default function LearningPage() {
  const [data, setData] = useState<Awaited<
    ReturnType<typeof fetchLearning>
  > | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    fetchLearning()
      .then(setData)
      .catch((e) => setErr(String(e)));
  }, []);
  return (
    <>
      <PageHeader
        eyebrow="Monitor"
        title="Learning"
        description="권장 조치가 실제로 증상을 해소했는지 자동 측정해 누적한 효과 이력 — 입증된 조치를 우선합니다."
      />
      <PageBody>
        {err ? (
          <EmptyState title="불러오지 못했습니다" description={err} />
        ) : !data ? (
          <div className="py-16 text-center text-sm text-slate-500">
            불러오는 중…
          </div>
        ) : data.fleet.length === 0 &&
          Object.keys(data.clusters).length === 0 ? (
          <EmptyState
            title="아직 학습된 결과가 없습니다"
            description="권장 조치가 적용되고 평가 윈도우가 지나면 효과 이력이 쌓입니다."
          />
        ) : (
          <div className="space-y-2">
            {data.fleet.map((r) => (
              <div
                key={`f:${r.symptom_class}:${r.action_class}`}
                className="flex justify-between rounded-lg border border-slate-800 bg-slate-900/40 p-3 text-sm"
              >
                <span className="text-slate-300">
                  {r.symptom_class} · {r.action_class}
                </span>
                <span className="text-slate-400">
                  {trackRecordLabel(r.successes, r.attempts)} · 신뢰도{" "}
                  {(confidence(r.successes, r.attempts) * 100).toFixed(0)}%
                </span>
              </div>
            ))}
          </div>
        )}
      </PageBody>
    </>
  );
}
```

- [ ] **Step 4: Add nav entry** in `frontend/src/components/app-shell.tsx` — find the array holding the "Map" / "Tasks" items and add `{ href: "/learning", label: "Learning", icon: <icon> }` matching the existing item shape (read the file to get the exact object shape + icon import).

- [ ] **Step 5: Typecheck + build**

Run: `cd frontend && npx tsc --noEmit && npm run build`
Expected: no type errors; static export succeeds.

- [ ] **Step 6: Add a Playwright smoke assertion** in `frontend/e2e/smoke.spec.ts` (mirror an existing page test):

```ts
test("learning page renders", async ({ page }) => {
  await page.goto("/learning");
  await expect(page.getByRole("heading", { name: "Learning" })).toBeVisible();
});
```

- [ ] **Step 7: Commit** (expect prettier to reformat on first attempt → `git add -A` and re-commit)

```bash
git add -A
git commit -m "feat(outcome-loop): Learning page + nav + confidence helper"
# if prettier rewrote files and aborted: git add -A && git commit -m "..." again
```

---

## Phase 2

### Task 10: RCA-sourced cases (DynamoDB)

**Files:**

- Modify: `data-pipeline/outcome_evaluator/case_opener.py` (add `open_rca_cases(query, ddb, table_name)`), and call it from `handler.lambda_handler`
- Modify: `cdk/stacks/data_stack.py` — grant the `agent-tasks` table read + pass `AGENT_TASKS_TABLE` env to `outcome_evaluator`
- Test: `tests/unit/data_pipeline/test_rca_case_opener.py`

**Interfaces:**

- Consumes: `classify_action` (Task 2); a DynamoDB `Table` resource; recently-`done` `auto_rca`/`manual_rca` rows whose `result.candidates[0].category` + `result.recommendations[0]` define the symptom + action.
- Produces: `open_rca_cases(query, ddb_table) -> int`. `symptom_class = 'rca:'+category`, `watch_metric` = top candidate's metric if present else NULL, `action_class = classify_action(recommendations[0])`.

- [ ] **Step 1: Write the failing test** (paginated scan + filter; mirror `_broadcast`'s scan-with-LastEvaluatedKey pattern)

```python
# tests/unit/data_pipeline/test_rca_case_opener.py
from outcome_evaluator import case_opener

class _FakeTable:
    def __init__(self, items): self._items = items
    def scan(self, **kw): return {"Items": self._items}

def test_opens_rca_case_with_inferred_action():
    inserts = []
    def query(sql, params=None):
        if sql.strip().upper().startswith("INSERT INTO REMEDIATION_CASES"):
            inserts.append(params)
        return []
    tbl = _FakeTable([{
        "task_id": "t1", "kind": "auto_rca", "status": "done", "cluster_id": "c1",
        "completed_at": "9999999999999",
        "result": {"candidates": [{"category": "lock_contention", "metric": "blocking_count"}],
                   "recommendations": ["인덱스를 추가해 잠금 경합을 줄이세요"]},
    }])
    n = case_opener.open_rca_cases(query, tbl)
    assert n == 1
    p = inserts[0]
    assert p["symptom_class"] == "rca:lock_contention"
    assert p["action_class"] == "index_add"
    assert p["watch_metric"] == "blocking_count"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/data_pipeline/test_rca_case_opener.py -v`
Expected: FAIL — `AttributeError: open_rca_cases`.

- [ ] **Step 3: Implement** — add to `case_opener.py`:

```python
def open_rca_cases(query, ddb_table) -> int:
    """Open rca:<category> cases from recently-completed RCA tasks. Best-effort;
    a missing/empty result just yields no case."""
    if ddb_table is None:
        return 0
    items, scan_kwargs = [], {}
    while True:  # paginate — never trust a single scan page
        resp = ddb_table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    opened = 0
    for it in items:
        if it.get("kind") not in ("auto_rca", "manual_rca") or it.get("status") != "done":
            continue
        res = it.get("result") or {}
        cands = res.get("candidates") or []
        recs = res.get("recommendations") or []
        if not cands:
            continue
        category = cands[0].get("category") or "unknown"
        metric = cands[0].get("metric")
        query(_INSERT, {
            "cluster_id": it.get("cluster_id"),
            "symptom_class": f"rca:{category}",
            "symptom_subject": category,
            "watch_metric": metric,
            "severity_at_open": None,
            "recommendation_text": recs[0] if recs else None,
            "action_class": classify_action(recs[0] if recs else "", category),
            "source": "rca_worker",
            "win_min": WIN_METRIC_MIN if metric else WIN_FINDING_MIN,
        })
        opened += 1
    return opened
```

Wire it in `handler.lambda_handler` (after `open_cases`): read `AGENT_TASKS_TABLE`, and if set, `ddb = boto3.resource("dynamodb").Table(name)`, then `opened += case_opener.open_rca_cases(q, ddb)`. Dedup across runs is handled by the same partial unique index. **Note:** to keep re-scans from re-opening cases for old completed tasks, filter the scan to `completed_at` within the last day (add a `FilterExpression`) — _do not add a `Limit`_ (Limit + FilterExpression silently drops rows; see prior DDB bug).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/data_pipeline/test_rca_case_opener.py -v`
Expected: PASS.

- [ ] **Step 5: CDK grant + env**, then synth:

```python
        # Phase 2: RCA-sourced cases read recently completed agent tasks.
        foundation.agent_tasks_table.grant_read_data(self.outcome_evaluator)
        self.outcome_evaluator.add_environment(
            "AGENT_TASKS_TABLE", foundation.agent_tasks_table.table_name)
```

(Confirm the exact attribute name for the agent-tasks table on `foundation` — grep `agent_tasks` in `cdk/stacks/foundation_stack.py`; `grant_task_enqueue` already exposes it, so reuse that accessor if `agent_tasks_table` isn't public.)

Run: `python -m pytest tests/cdk/test_synth.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add data-pipeline/outcome_evaluator/case_opener.py cdk/stacks/data_stack.py tests/unit/data_pipeline/test_rca_case_opener.py
git commit -m "feat(outcome-loop): RCA-sourced cases from agent-tasks (phase 2)"
```

---

### Task 11: RCA narrative prompt injection

**Files:**

- Modify: `mcp-servers/mcp_servers/workers/task_worker.py` — `_narrative()` (~line 197)
- Test: `tests/unit/mcp_servers/workers/test_narrative_history.py`

**Interfaces:**

- Consumes: `remediation_outcomes_agg` via `CacheClient.execute`; the top RCA candidate's `category`.
- Produces: when history exists, the `_narrative` prompt includes a "과거 효과 이력" line built from agg rows; the model is told to cite it, not invent it. No behavior change when there's no history.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/mcp_servers/workers/test_narrative_history.py
from unittest.mock import MagicMock
from mcp_servers.workers import task_worker

def test_history_line_built_from_agg():
    cache = MagicMock()
    cache.execute.return_value.rows = [
        {"action_class": "index_add", "successes": 4, "attempts": 5},
        {"action_class": "param_change", "successes": 1, "attempts": 3},
    ]
    line = task_worker._history_line(cache, "c1", "lock_contention")
    assert "index_add" in line and "4/5" in line
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/mcp_servers/workers/test_narrative_history.py -v`
Expected: FAIL — `AttributeError: _history_line`.

- [ ] **Step 3: Implement** — add a helper and call it inside `_narrative`:

```python
def _history_line(cache, cluster_id: str, category: str) -> str:
    """One-line track record for an 'rca:<category>' symptom, cluster then fleet
    fallback. Empty string when there's no history (caller omits the line)."""
    sclass = f"rca:{category}"
    try:
        rows = cache.execute(
            "SELECT action_class, successes, attempts FROM remediation_outcomes_agg "
            "WHERE cluster_id = :cid AND symptom_class = :sc AND attempts > 0 "
            "ORDER BY attempts DESC LIMIT 5",
            {"cid": cluster_id, "sc": sclass},
        ).rows
        if not rows:
            rows = cache.execute(
                "SELECT action_class, successes, attempts FROM remediation_outcomes_agg "
                "WHERE cluster_id = '*' AND symptom_class = :sc AND attempts > 0 "
                "ORDER BY attempts DESC LIMIT 5",
                {"sc": sclass},
            ).rows
    except Exception:
        return ""
    if not rows:
        return ""
    parts = [f"{r['action_class']} {int(r['successes'])}/{int(r['attempts'])}" for r in rows]
    return "과거 효과 이력(조치 성공/시도): " + ", ".join(parts)
```

In `_narrative`, after building `lines`, fetch the category from `candidates[0]` and, if `_history_line` returns non-empty, append it to the prompt with the instruction: `"\n위 '과거 효과 이력'을 근거로 우선순위를 정하되, 이력에 없는 효과는 단정하지 마세요."`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/mcp_servers/workers/test_narrative_history.py tests/unit/mcp_servers/workers/test_task_worker.py -v`
Expected: PASS (existing task_worker tests still green).

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/mcp_servers/workers/task_worker.py tests/unit/mcp_servers/workers/test_narrative_history.py
git commit -m "feat(outcome-loop): inject remediation history into RCA narrative"
```

---

### Task 12: `get_remediation_history` MCP tool

**Files:**

- Create: `mcp-servers/mcp_servers/incident/tools/remediation_history.py`
- Modify: `mcp-servers/mcp_servers/incident/handler.py` (register impl), the incident tool-definitions/schema, and the gateway tool list
- Test: `tests/unit/mcp_servers/incident/test_remediation_history.py` + the existing handler↔schema parity test

**Interfaces:**

- Consumes: `CacheClient`; tables `remediation_outcomes_agg`, `remediation_cases`.
- Produces: `get_remediation_history_impl(cache, cluster_id: str, symptom_class: str = "") -> dict` → `{actions: [{action_class, successes, attempts}], recent: [...]}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/mcp_servers/incident/test_remediation_history.py
from unittest.mock import MagicMock
from mcp_servers.incident.tools.remediation_history import get_remediation_history_impl

def test_returns_actions_for_symptom():
    cache = MagicMock()
    cache.execute.return_value.rows = [{"action_class": "index_add", "successes": 4, "attempts": 5}]
    out = get_remediation_history_impl(cache, "c1", "finding:query_regression")
    assert out["actions"][0]["action_class"] == "index_add"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/mcp_servers/incident/test_remediation_history.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the impl** + register it exactly like a sibling incident tool (e.g. `find_similar_incidents`): add to the handler's tool map, the JSON tool-definition (name `get_remediation_history`, params `cluster_id` required, `symptom_class` optional), and the gateway tool list so semantic search can route it.

```python
# mcp-servers/mcp_servers/incident/tools/remediation_history.py
"""MCP tool: expose the learned remediation track record to the chat agent."""


def get_remediation_history_impl(cache, cluster_id: str, symptom_class: str = "") -> dict:
    where = "cluster_id = :cid"
    params = {"cid": cluster_id}
    if symptom_class:
        where += " AND symptom_class = :sc"
        params["sc"] = symptom_class
    actions = cache.execute(
        f"SELECT action_class, symptom_class, successes, attempts, last_outcome "
        f"FROM remediation_outcomes_agg WHERE {where} AND attempts > 0 "
        f"ORDER BY attempts DESC LIMIT 50", params,
    ).rows
    recent = cache.execute(
        "SELECT symptom_class, action_class, status, evaluated_at FROM remediation_cases "
        "WHERE cluster_id = :cid AND status IN ('resolved','persisted') "
        "ORDER BY evaluated_at DESC LIMIT 20", {"cid": cluster_id},
    ).rows
    return {"actions": actions, "recent": recent}
```

- [ ] **Step 4: Run impl test + parity test**

Run: `python -m pytest tests/unit/mcp_servers/incident/test_remediation_history.py -v` → PASS.
Run the existing incident handler↔schema parity test (grep `tests/` for the parity test that asserts every handler tool has a gateway schema entry) → must PASS with the new tool present on both sides.

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/mcp_servers/incident/tools/remediation_history.py mcp-servers/mcp_servers/incident/handler.py tests/unit/mcp_servers/incident/test_remediation_history.py
# plus the tool-definition / gateway schema files you edited
git commit -m "feat(outcome-loop): get_remediation_history MCP tool (phase 2)"
```

---

## Self-review

**Spec coverage:**

- §2 data model → Task 1. §3 case opening → Task 3 (findings+anomalies) + Task 10 (RCA). §4 evaluator (metric recovery + finding recurrence + false-resolved guard + agg, cluster + '\*') → Task 4. Scheduled Lambda → Tasks 5–6. §5 consumers: deterministic re-rank+badge → Task 7; prompt injection → Task 11; agent tool → Task 12. §6 UI → Tasks 8–9. §7 honesty (cold-start fleet fallback, attribution-as-hint, no auto-apply) → enforced in Tasks 4/7/8. §8 scope/phasing → task split. **action_class classifier** (§3) → Task 2. **All covered.**
- One deliberate deviation, documented in Architecture: case opening is pull-based in the evaluator (not per-emitter), which removes the spec's "copy classifier into both packages" note — the classifier lives once (Task 2).

**Placeholder scan:** every code step has real code; commands have expected output. Two spots intentionally say "read the sibling first and mirror it" (the `/multi-cluster/overview` auth setup in Task 8; the nav item shape in Task 9; the `api-client` `authedFetch` usage) — these are reads against existing code whose exact shape must match, not deferred logic.

**Type/name consistency:** `query(sql, params)` callable shape is consistent across Tasks 3/4/5/7/8. `remediation_cases` / `remediation_outcomes_agg` columns match between Task 1 (DDL), Task 4 (writes), Task 7/8 (reads). `classify_action(text, category="")` signature consistent across Tasks 2/3/10. `confidence(successes, attempts)` / `trackRecordLabel` consistent in Task 9. Aggregate key `(cluster_id, symptom_class, action_class)` consistent everywhere; findings badge aggregates on `symptom_class` only (no read-time classifier), noted in Task 7.
