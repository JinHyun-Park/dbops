# Task Execution Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Surface per-task execution trace (steps + timing + examined signals) and fleet-level task statistics on the Agent Tasks subsystem.

**Architecture:** task_worker records a lightweight `trace` (step/tool/ms/detail) + `duration_ms` on each task row when it finishes; `GET /api/tasks/{id}` already returns the whole row so the trace surfaces for free; a new `GET /api/tasks/stats` aggregates recent tasks; the `/tasks` page renders the trace + examined signals + a stats strip.

**Tech Stack:** Python 3.12 Lambdas (DynamoDB via boto3 resource), AWS CDK (Python), Next.js 16 static export (TanStack Query-free fetch helpers), pytest.

## Global Constraints

- CDK-only infrastructure — never modify AWS resources directly (AGENTS.md).
- Non-breaking: existing task rows (no `trace`/`duration_ms`) must render and aggregate fine; the deterministic RCA engine (`diagnose_root_cause`) is unchanged; existing task_worker tests stay valid (the `_finish` write count for a done task stays 2: claim + finish).
- Adding the `/api/tasks/stats` route REQUIRES regenerating `frontend/public/openapi.json` via `python3 tools/openapi_gen.py` (the `test_openapi_spec` test gates this).
- DynamoDB rows reject Python `float` — any numeric written must pass through the worker's existing `_ddb_safe` (ints are fine; avoid floats).
- Korean translation scope: DB jargon stays English (Replica Lag, IOPS…); human-facing labels/empty-states/`detail` strings are Korean. Trace step labels are Korean ("진단", "서술 생성", "헬스 다이제스트").
- Numbers ≥1000 in the frontend use the existing `fmtDecimal`/`fmtExact` helpers; durations in ms/seconds may use plain formatting.
- Commits: conventional subject; NO `Co-Authored-By: Claude` trailer; do NOT reference internal roadmaps/wikis. Frontend commits hit a prettier pre-commit hook — if it reformats, `git add -A` and re-commit (do not chain commit+push).
- No secrets or query bodies in the trace — tool names, counts, durations, short Korean `detail` only.

---

## File Structure

**Increment 1 — Backend trace (agent stack)**

- Modify: `mcp-servers/mcp_servers/workers/task_worker.py` — record `trace` + `duration_ms`; extend `_finish`.
- Test: `tests/unit/mcp_servers/workers/test_task_worker.py` (extend).

**Increment 2 — API stats (agent stack)**

- Modify: `api/tasks/handler.py` — `GET /api/tasks/stats` branch + `_stats()`.
- Modify: `cdk/stacks/agent_stack.py` — register `GET /api/tasks/stats`.
- Modify: `frontend/public/openapi.json` — regenerated.
- Test: `tests/unit/api/test_tasks_stats.py` (new).

**Increment 3 — Frontend (tasks page)**

- Modify: `frontend/src/lib/api-client.ts` — `AgentTask.trace`/`duration_ms`, `TaskStats`, `fetchTaskStats()`.
- Modify: `frontend/src/app/tasks/page.tsx` — trace/signals/duration in `TaskRow`; stats strip.

---

## Increment 1 — Backend trace

### Task 1: task_worker records trace + duration

**Files:**

- Modify: `mcp-servers/mcp_servers/workers/task_worker.py`
- Test: `tests/unit/mcp_servers/workers/test_task_worker.py`

**Interfaces:**

- Produces: task rows now carry `trace: list[{step,tool,ms,detail}]` and `duration_ms: int` (read by the frontend in Increment 3).
- `_finish(task_id, *, status, result=None, summary=None, error=None, ticket_url=None, trace=None, duration_ms=None)`.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/mcp_servers/workers/test_task_worker.py`:

```python
def test_auto_rca_records_trace_and_duration():
    table = MagicMock()
    rca = {"status": "ok", "candidates": [{"summary": "x", "category": "event"}],
           "signals_examined": {"events": 3, "metric_spikes": 2}}
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", return_value=rca), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 1
    finish = table.update_item.call_args_list[-1].kwargs["ExpressionAttributeValues"]
    trace = finish[":trace"]
    assert isinstance(trace, list) and any(s["tool"] == "diagnose_root_cause" for s in trace)
    assert ":dur" in finish  # duration recorded


def test_failed_task_still_records_trace_and_duration():
    table = MagicMock()
    with patch.object(tw, "_table", return_value=table), \
         patch.object(tw, "_get_cache", return_value=MagicMock()), \
         patch.object(tw, "diagnose_root_cause_impl", side_effect=RuntimeError("boom")), \
         patch.object(tw, "_broadcast"):
        out = tw.lambda_handler({"Records": [_insert()]}, None)
    assert out["processed"] == 0
    finish = table.update_item.call_args_list[-1].kwargs["ExpressionAttributeValues"]
    assert finish[":s"] == "failed"
    assert ":dur" in finish  # duration recorded even on failure
```

- [ ] **Step 2: Run, verify they fail** — `python3 -m pytest tests/unit/mcp_servers/workers/test_task_worker.py -q` (KeyError `:trace`/`:dur`).

- [ ] **Step 3: Implement**

In `task_worker.py`:

1. Extend `_finish` signature + body (after the `summary`/`ticket_url` blocks, before `error`):

```python
def _finish(task_id, *, status, result=None, summary=None, error=None, ticket_url=None, trace=None, duration_ms=None):
    ...
    if trace is not None:
        vals[":trace"] = _ddb_safe(trace)
        sets.append("#trc = :trace")
        names["#trc"] = "trace"
    if duration_ms is not None:
        vals[":dur"] = int(duration_ms)
        sets.append("duration_ms = :dur")
    if error is not None:
        ...
```

(`trace` is a reserved word in DynamoDB → alias `#trc`. `duration_ms` is not reserved.)

2. Make `_run_rca` / `_run_report` return a third element `steps` (list of trace dicts), timing each generator with `time.time()` deltas:

```python
def _run_rca(cluster_id: str):
    steps = []
    t = time.time()
    res = diagnose_root_cause_impl(_get_cache(), cluster_id)
    cands = res.get("candidates", []) if isinstance(res, dict) else []
    examined = res.get("signals_examined", {}) if isinstance(res, dict) else {}
    nsrc = len([k for k, v in examined.items() if v]) if isinstance(examined, dict) else 0
    steps.append({"step": "진단", "tool": "diagnose_root_cause",
                  "ms": int((time.time() - t) * 1000),
                  "detail": f"{nsrc}개 소스 검사 · 후보 {len(cands)}"})
    if isinstance(res, dict):
        t = time.time()
        narr = _narrative(cluster_id, res)
        if narr:
            res.update(narr)
            steps.append({"step": "서술 생성", "tool": "bedrock",
                          "ms": int((time.time() - t) * 1000),
                          "detail": "한국어 narrative+권장조치"})
        else:
            steps.append({"step": "서술 생성", "tool": "bedrock", "ms": 0,
                          "detail": "모델 미설정/실패 — 스킵"})
    summary = (cands[0].get("summary") or cands[0].get("category") or "신호 감지") if cands \
        else "자동 수집 신호에서 뚜렷한 원인 미발견 — 수동 점검 권장"
    return res, summary, steps
```

Mirror for `_run_report` (one step `{"step":"헬스 다이제스트","tool":"health_status","ms":...,"detail":...}`); return `(report, summary, steps)`.

3. In `lambda_handler`, time the whole run and thread trace/duration through:

```python
        t0 = time.time()
        try:
            if kind in ("auto_rca", "manual_rca"):
                result, summary, steps = _run_rca(cluster_id)
            elif kind == "scheduled_report":
                result, summary, steps = _run_report(cluster_id)
            else:
                raise NotImplementedError(...)
            ticket_url = _maybe_create_ticket(task_id, cluster_id, kind, summary, result)
            _finish(task_id, status="done", result=result, summary=summary,
                    ticket_url=ticket_url, trace=steps,
                    duration_ms=int((time.time() - t0) * 1000))
            ...
        except Exception as e:
            ...
            _finish(task_id, status="failed", summary=f"작업 실패: {type(e).__name__}",
                    error=str(e), duration_ms=int((time.time() - t0) * 1000))
```

(On failure `steps` may be undefined — do NOT pass `trace` there; just `duration_ms`. If you want partial trace on failure, initialize `steps=[]` before the try and append inside `_run_*`; simplest correct version: only duration on the failure path. The failure test only asserts `:dur`.)

- [ ] **Step 4: Run tests** — `python3 -m pytest tests/unit/mcp_servers/workers -q` → all pass (existing + 2 new). Confirm `test_auto_rca_happy_path` still asserts 2 writes (trace folds into the single finish write).

- [ ] **Step 5: Commit** — `git add mcp-servers/mcp_servers/workers/task_worker.py tests/unit/mcp_servers/workers/test_task_worker.py` ; `git commit -m "feat(tasks): record per-task execution trace + duration"`

---

## Increment 2 — API stats

### Task 2: GET /api/tasks/stats

**Files:**

- Modify: `api/tasks/handler.py`
- Modify: `cdk/stacks/agent_stack.py`
- Test: `tests/unit/api/test_tasks_stats.py` (new)

**Interfaces:**

- Produces: `GET /api/tasks/stats` → `{total, by_status, by_kind, success_rate, avg_duration_ms, recent_failures}` (read by `fetchTaskStats` in Increment 3).

- [ ] **Step 1: Write failing test** — `tests/unit/api/test_tasks_stats.py`:

```python
import json
from unittest.mock import patch
import importlib

h = importlib.import_module("api.tasks.handler")


def _evt(path="/api/tasks/stats", method="GET"):
    return {"rawPath": path, "requestContext": {"http": {"method": method}}}


def test_stats_aggregates(monkeypatch):
    rows = [
        {"status": "done", "kind": "auto_rca", "duration_ms": 400},
        {"status": "done", "kind": "manual_rca", "duration_ms": 600},
        {"status": "failed", "kind": "auto_rca"},
        {"status": "running", "kind": "scheduled_report"},
    ]
    with patch.object(h, "_recent_for_stats", return_value=rows):
        resp = h.lambda_handler(_evt(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["total"] == 4
    assert body["by_status"]["done"] == 2
    assert body["by_kind"]["auto_rca"] == 2
    # success_rate = done / (done+failed) finished tasks = 2/3
    assert round(body["success_rate"], 2) == 0.67
    assert body["avg_duration_ms"] == 500  # mean of done durations
    assert body["recent_failures"] == 1


def test_stats_empty_is_zero_safe(monkeypatch):
    with patch.object(h, "_recent_for_stats", return_value=[]):
        resp = h.lambda_handler(_evt(), None)
    body = json.loads(resp["body"])
    assert body["total"] == 0 and body["success_rate"] == 0 and body["avg_duration_ms"] == 0
```

- [ ] **Step 2: Run, verify fail** — `python3 -m pytest tests/unit/api/test_tasks_stats.py -q` (no `/stats` branch / no `_recent_for_stats`).

- [ ] **Step 3: Implement** in `api/tasks/handler.py`:

Add a recent-rows helper + a stats builder, and a dispatch branch. Use the existing `recency-index` query (mirror `_list`'s no-cluster branch):

```python
def _recent_for_stats(limit=500):
    resp = _table().query(
        IndexName="recency-index",
        KeyConditionExpression=Key("record_type").eq("task"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def _stats():
    rows = _recent_for_stats()
    by_status, by_kind, durs = {}, {}, []
    for r in rows:
        st = str(r.get("status", "unknown"))
        by_status[st] = by_status.get(st, 0) + 1
        kd = str(r.get("kind", "unknown"))
        by_kind[kd] = by_kind.get(kd, 0) + 1
        if st == "done" and r.get("duration_ms") is not None:
            try:
                durs.append(int(r["duration_ms"]))
            except (TypeError, ValueError):
                pass
    done = by_status.get("done", 0)
    failed = by_status.get("failed", 0)
    finished = done + failed
    return {
        "total": len(rows),
        "by_status": by_status,
        "by_kind": by_kind,
        "success_rate": round(done / finished, 4) if finished else 0,
        "avg_duration_ms": int(sum(durs) / len(durs)) if durs else 0,
        "recent_failures": failed,
    }
```

In `lambda_handler`, near the top of the GET handling (before the `task_id`/list branches):

```python
    raw_path = event.get("rawPath") or event.get("path") or ""
    if method == "GET" and raw_path.endswith("/stats"):
        try:
            return {"statusCode": 200, "headers": headers, "body": json.dumps(_stats(), default=str)}
        except Exception as e:
            return {"statusCode": 500, "headers": headers, "body": json.dumps({"error": str(e)[:200]})}
```

(Place it BEFORE the `if method == "GET" and task_id:` branch. `/stats` has no `{id}` path param so it won't collide, but the explicit suffix check keeps it unambiguous.)

- [ ] **Step 4: Register route** in `cdk/stacks/agent_stack.py` — find where `/api/tasks` routes are registered (`GET /api/tasks`, `GET /api/tasks/{id}`, `POST /api/tasks`) and add, reusing the SAME integration var those use:

```python
self.api.add_routes(
    path="/api/tasks/stats",
    methods=[apigwv2.HttpMethod.GET],
    integration=<the same tasks integration used by the other /api/tasks routes>,
)
```

Register `/api/tasks/stats` BEFORE `/api/tasks/{id}` if the framework is order-sensitive (a literal segment vs a path param) — HTTP API matches literals over greedy params, but register stats adjacent to the others for clarity. Copy the exact integration variable name from the neighbouring task route registration; do not invent one.

- [ ] **Step 5: Validate synth + regenerate openapi**

  - `cd cdk && cdk synth dbops-dev-agent --quiet` → exit 0.
  - `python3 tools/openapi_gen.py` ; confirm `/api/tasks/stats` is in `frontend/public/openapi.json`.
  - `python3 -m pytest tests/unit/test_openapi_spec.py -q` → pass.

- [ ] **Step 6: Run tests** — `python3 -m pytest tests/unit/api -q` (new stats tests + no regression).

- [ ] **Step 7: Commit** — add `api/tasks/handler.py cdk/stacks/agent_stack.py frontend/public/openapi.json tests/unit/api/test_tasks_stats.py` ; `git commit -m "feat(tasks): /api/tasks/stats aggregate endpoint"`

---

## Increment 3 — Frontend

### Task 3: api-client — trace/duration types + fetchTaskStats

**Files:**

- Modify: `frontend/src/lib/api-client.ts`

**Interfaces:**

- Produces: `AgentTask` gains `trace?: TraceStep[]` and `duration_ms?: number`; `TaskStats` type; `fetchTaskStats(): Promise<TaskStats>`.

- [ ] **Step 1: Extend types + add fetch** (find the existing `AgentTask` interface and `fetchTasks`; use the same `authedFetch` + `api()` helpers):

```typescript
export interface TraceStep {
  step: string;
  tool: string;
  ms: number;
  detail: string;
}
export interface TaskStats {
  total: number;
  by_status: Record<string, number>;
  by_kind: Record<string, number>;
  success_rate: number;
  avg_duration_ms: number;
  recent_failures: number;
}
// add to AgentTask: trace?: TraceStep[]; duration_ms?: number;

export async function fetchTaskStats(): Promise<TaskStats> {
  const res = await authedFetch(await api(`/api/tasks/stats`));
  if (!res.ok) throw new Error(`작업 통계 조회 실패 (상태 ${res.status})`);
  return res.json();
}
```

- [ ] **Step 2: Typecheck** — `cd frontend && npx --no-install tsc --noEmit` → no errors.
- [ ] **Step 3: Commit** — `git add frontend/src/lib/api-client.ts` ; `git commit -m "feat(tasks): api-client — task stats + trace types"`

### Task 4: tasks page — trace/signals/duration + stats strip

**Files:**

- Modify: `frontend/src/app/tasks/page.tsx`

**Interfaces:**

- Consumes: `fetchTaskStats`, `AgentTask.trace`/`duration_ms`, `result.signals_examined`/`skipped`.

- [ ] **Step 1: Stats strip** — in `TasksPage`, add `const [stats, setStats] = useState<TaskStats | null>(null);` and load it in the existing load effect (call `fetchTaskStats().then(setStats).catch(() => {})`, refreshed on the same 5s interval as `load`). Render a compact strip above the task list: 총 작업(`stats.total`), 성공률(`Math.round(stats.success_rate*100)%`), 평균 소요(`stats.avg_duration_ms` ms→ `${(ms/1000).toFixed(1)}s`), 종류별 카운트. Numbers ≥1000 via `fmtDecimal`. Hide/skeleton when `stats` null.

- [ ] **Step 2: Trace + signals in `TaskRow` detail** — inside the `open && (done||failed)` block, after the existing narrative/candidates/lines, add:

  - **실행 추적**: if `task.trace?.length`, an ordered list of `step` — `tool` · `detail` · `{ms}ms` (monospace, muted). Show total: `task.duration_ms` as `${(duration_ms/1000).toFixed(1)}s` when present.
  - **검사한 신호** (RCA only): if `task.result?.signals_examined`, a small key→count table (source → count); list `skipped` sources muted if present.
    Reuse the existing detail container styling (border-zinc / text-xs / font-mono). Korean labels.

- [ ] **Step 3: Build** — `cd frontend && npm run build` → exit 0, `/tasks` in route list.
- [ ] **Step 4: Commit (mind prettier)** — `git add frontend/src/app/tasks/page.tsx` ; `git commit -m "feat(tasks): execution trace + examined signals + stats strip on /tasks"` (if prettier reformats: `git add -A` then re-run).

---

## Self-Review

- Spec §2.1 (trace/duration_ms additive) → Task 1. ✓
- Spec §3.1 (/api/tasks/stats) → Task 2. ✓
- Spec §3.3 (frontend trace/signals/duration + stats strip) → Tasks 3–4. ✓
- Spec §6 (tests) → Task 1 unit, Task 2 unit + parity, Task 4 build + e2e checkpoint. ✓
- Type consistency: `TraceStep {step,tool,ms,detail}` (Task 3) == worker step dict (Task 1) == `trace` attribute. `TaskStats` (Task 3) == `_stats()` return (Task 2). ✓
- Non-breaking: `_finish` write count unchanged (trace folds into the one finish write); old rows lack `trace` → frontend guards with `?.`; stats tolerates missing `duration_ms`. ✓
