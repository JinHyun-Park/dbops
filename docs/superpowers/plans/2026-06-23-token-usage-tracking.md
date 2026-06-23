# Token Usage Tracking + Per-Session Error Surfacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface Bedrock token usage two ways — a fleet-aggregate read-only view (CloudWatch, by model + over time) and per-session token totals + last error captured from the agent stream — without ever breaking the chat stream.

**Architecture:** `GET /api/cost?view=tokens` reads CloudWatch `AWS/Bedrock` token metrics. The agent emits a terminal `usage` SSE event; the frontend accumulates it onto the session and persists via the existing `chat_sessions` PUT; the session list surfaces per-session tokens + errors.

**Tech Stack:** Python 3.12 Lambda (CloudWatch GetMetricData, DynamoDB), AgentCore Runtime (Strands `stream_async`), Next.js 16 (static export), TypeScript.

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **Chat stream is sacrosanct:** all agent-side usage capture/emit is FAIL-SAFE — wrapped so any error is swallowed and the answer always streams. A missing/changed Strands usage shape simply omits the `usage` marker.
- **Agent deploy sensitivity:** `agent/server.py` runs in the AgentCore Runtime container. Do NOT leave a `__pycache__` under `agent/` at deploy time (it breaks the Runtime deploy). Validate agent code with `ast.parse`, not import-exec, in the deploy path; a unit test that imports the agent helper must clean `agent/__pycache__` (the deploy step also cleans it). AgentCore env/code changes take ~10 min to reach a warm container. See memory: agentcore-no-pycache.
- **Additive only:** the new `chat_sessions` fields (`total_input_tokens`, `total_output_tokens`, `turn_count`, `last_error`) are optional — old sessions and non-usage turns omit them, and existing session behavior is unchanged.
- **Fleet view scope honesty:** CloudWatch Bedrock token metrics are not DBOps-tag-filterable — the tokens view is account-wide Bedrock usage by model; say so in the response `note` (mirrors how the cost views disclose untagged scope).
- **Korean UI copy** for explanatory/empty-state text; keep model ids / token jargon as-is.

---

### Task 1: Fleet token view — `GET /api/cost?view=tokens` (backend + IAM + UI)

**Files:**

- Modify: `api/cost/handler.py` (add `_handle_tokens_view` + a `view == "tokens"` branch in `lambda_handler`)
- Modify: `cdk/stacks/agent_stack.py` (add CloudWatch actions to the `CostApi` Lambda role, ~line 1373)
- Modify: `frontend/src/lib/api-client.ts` (cost fetch passes `view`; type the tokens response)
- Modify: `frontend/src/app/cost/page.tsx` (add a "토큰" view + render)
- Test: `tests/unit/api/test_cost_tokens.py`

**Interfaces:**

- Produces: `GET /api/cost?view=tokens&days=N` → `{"view":"tokens","days":N,"by_model":[{"model","input","output","total"}],"daily":[{"date","input","output"}],"note":str}`.

- [ ] **Step 1: Read** the current `api/cost/handler.py` — the `lambda_handler` view dispatch (`view = (qs.get("view") or "bedrock").lower()` ~line 386, branches for `rds`/`platform`), the `_response`/`_cors` helpers, and the `datetime`/`timedelta` imports (already present). Confirm `boto3` is imported.

- [ ] **Step 2: Write the failing tests.** Create `tests/unit/api/test_cost_tokens.py` (load the handler via importlib like the sibling cost tests; read `tests/unit/api/test_cost_rds.py` for the existing pattern). Tests with a mocked CloudWatch client:

```python
import importlib.util, json
from pathlib import Path
from unittest.mock import MagicMock, patch

_H = Path(__file__).resolve().parents[3] / "api" / "cost" / "handler.py"
_s = importlib.util.spec_from_file_location("cost_handler", _H)
handler = importlib.util.module_from_spec(_s); _s.loader.exec_module(handler)


def _event(view="tokens", days="30"):
    return {"requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"view": view, "days": days}}


def _fake_cw():
    cw = MagicMock()
    cw.list_metrics.return_value = {
        "Metrics": [
            {"Dimensions": [{"Name": "ModelId", "Value": "anthropic.claude-sonnet-4-6"}]},
            {"Dimensions": [{"Name": "ModelId", "Value": "anthropic.claude-opus-4-8"}]},
        ]
    }
    # get_metric_data returns one result per (model, input|output) query id.
    def _gmd(MetricDataQueries, **kw):
        results = []
        for q in MetricDataQueries:
            results.append({"Id": q["Id"], "Timestamps": [], "Values": [100.0]})
        return {"MetricDataResults": results}
    cw.get_metric_data.side_effect = _gmd
    return cw


def test_tokens_view_aggregates_by_model():
    with patch.object(handler.boto3, "client") as mk:
        mk.return_value = _fake_cw()
        r = handler.lambda_handler(_event())
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["view"] == "tokens"
    models = {m["model"] for m in body["by_model"]}
    assert "anthropic.claude-sonnet-4-6" in models
    assert all("input" in m and "output" in m and "total" in m for m in body["by_model"])
    assert "note" in body


def test_tokens_view_empty_metrics_is_valid():
    cw = MagicMock()
    cw.list_metrics.return_value = {"Metrics": []}
    cw.get_metric_data.return_value = {"MetricDataResults": []}
    with patch.object(handler.boto3, "client") as mk:
        mk.return_value = cw
        r = handler.lambda_handler(_event())
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["by_model"] == []


def test_default_view_does_not_call_cloudwatch():
    # ?view=bedrock (default) must not touch the tokens path / cloudwatch client.
    with patch.object(handler, "_handle_tokens_view") as th:
        # ce path will try real CE; we only assert the tokens handler isn't invoked.
        try:
            handler.lambda_handler(_event(view="bedrock"))
        except Exception:
            pass
        th.assert_not_called()
```

Run: `python -m pytest tests/unit/api/test_cost_tokens.py -q` → FAIL (no `_handle_tokens_view`).

- [ ] **Step 3: Implement `_handle_tokens_view`.** In `api/cost/handler.py`, add the handler (place near `_handle_platform_view`):

```python
def _handle_tokens_view(start, end, days):
    """Fleet Bedrock token usage by model + daily series, from CloudWatch
    AWS/Bedrock metrics. NOTE: these metrics are not tag-filterable, so this is
    account-wide Bedrock token usage (same untagged scope the cost views note)."""
    cw = boto3.client("cloudwatch")
    # Discover model ids from the InputTokenCount metric's ModelId dimension.
    try:
        metrics = cw.list_metrics(Namespace="AWS/Bedrock", MetricName="InputTokenCount").get("Metrics", [])
    except Exception as e:
        return _response(200, {"view": "tokens", "days": days, "by_model": [], "daily": [],
                               "note": f"CloudWatch 토큰 메트릭 조회 실패: {type(e).__name__}"})
    model_ids = sorted({
        d["Value"] for m in metrics for d in m.get("Dimensions", []) if d["Name"] == "ModelId"
    })
    if not model_ids:
        return _response(200, {"view": "tokens", "days": days, "by_model": [], "daily": [],
                               "note": "Bedrock 토큰 메트릭 없음 — 아직 모델 호출 기록이 없거나 메트릭 전파 전입니다."})

    # Build GetMetricData queries: per model, Input + Output, Sum, daily period.
    import datetime as _dt
    queries, idmap = [], {}
    for i, mid in enumerate(model_ids):
        safe = "".join(c if c.isalnum() else "_" for c in mid)[:40]
        for kind, metric in (("input", "InputTokenCount"), ("output", "OutputTokenCount")):
            qid = f"m{i}_{kind}"
            idmap[qid] = (mid, kind)
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {"Namespace": "AWS/Bedrock", "MetricName": metric,
                               "Dimensions": [{"Name": "ModelId", "Value": mid}]},
                    "Period": 86400, "Stat": "Sum",
                },
                "ReturnData": True,
            })
    start_dt = _dt.datetime.combine(start, _dt.time.min)
    end_dt = _dt.datetime.combine(end, _dt.time.min)
    # GetMetricData caps at 500 queries/call; our model count is tiny, so one call.
    resp = cw.get_metric_data(MetricDataQueries=queries[:500], StartTime=start_dt, EndTime=end_dt,
                              ScanBy="TimestampAscending")
    totals = {mid: {"input": 0.0, "output": 0.0} for mid in model_ids}
    daily: dict = {}
    for res in resp.get("MetricDataResults", []):
        mid, kind = idmap.get(res["Id"], (None, None))
        if mid is None:
            continue
        for ts, val in zip(res.get("Timestamps", []), res.get("Values", [])):
            totals[mid][kind] += val
            day = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
            daily.setdefault(day, {"input": 0.0, "output": 0.0})[kind] += val
        if not res.get("Timestamps"):
            # some mocks/edge return Values without Timestamps — fold into totals
            for val in res.get("Values", []):
                totals[mid][kind] += val
    by_model = [{"model": mid, "input": int(t["input"]), "output": int(t["output"]),
                 "total": int(t["input"] + t["output"])}
                for mid, t in totals.items()]
    by_model.sort(key=lambda m: m["total"], reverse=True)
    daily_list = [{"date": d, "input": int(v["input"]), "output": int(v["output"])}
                  for d, v in sorted(daily.items())]
    return _response(200, {"view": "tokens", "days": days, "by_model": by_model, "daily": daily_list,
                           "note": "계정 전체 Bedrock 토큰 사용량(모델별) — CloudWatch 메트릭은 태그 필터 불가."})
```

And add the dispatch branch in `lambda_handler`, right after the `platform` branch (~line 391):

```python
    if view == "tokens":
        return _handle_tokens_view(start, end, days)
```

(`start`/`end`/`days` are already computed above the dispatch. The `ce` client is built before the dispatch but `_handle_tokens_view` doesn't use it — harmless.)

- [ ] **Step 4: Run backend tests.** `python -m pytest tests/unit/api/test_cost_tokens.py -q` → PASS. Then `python -m pytest tests/unit/api -q` → no regression.

- [ ] **Step 5: Add CloudWatch IAM.** In `cdk/stacks/agent_stack.py`, the `cost_lambda.add_to_role_policy(...)` (~line 1373) currently lists only `ce:*`. Add a second statement after it:

```python
        cost_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"],
            resources=["*"],  # CloudWatch GetMetricData/ListMetrics don't support resource scoping
        ))
```

Run: `python -m pytest tests/cdk/test_synth.py -q` → PASS.

- [ ] **Step 6: Frontend — api-client + cost page "토큰" view.** In `frontend/src/lib/api-client.ts`, find the cost fetch (search `"/api/cost"`); add an optional `view` param threaded into the query string + a `TokensCost` type `{ view: string; days: number; by_model: {model:string;input:number;output:number;total:number}[]; daily: {date:string;input:number;output:number}[]; note?: string }`. In `frontend/src/app/cost/page.tsx`, read the existing view-toggle (Bedrock/RDS/Platform) and add a "토큰" option that fetches `view=tokens` and renders: a by-model table/bar (model, input, output, total — use `fmtDecimal`/comma formatting for the counts per the number-formatting rule) and a daily time-series chart (reuse the chart component the page already uses for cost-over-time). Surface the `note` as a small caption. Korean labels.

Run: `cd frontend && npm run build` → PASS.

- [ ] **Step 7: Commit.**

```bash
git add api/cost/handler.py cdk/stacks/agent_stack.py frontend/src/lib/api-client.ts frontend/src/app/cost/page.tsx tests/unit/api/test_cost_tokens.py
git commit -m "feat(token-usage): fleet Bedrock token view (GET /api/cost?view=tokens) + UI"
```

---

### Task 2: Agent usage emission — terminal `usage` SSE event

**Files:**

- Modify: `agent/server.py` (add `_extract_usage` + emit a terminal usage marker in the stream loop)
- Test: `tests/unit/agent/test_extract_usage.py` (create; clean `agent/__pycache__` in teardown)

**Interfaces:**

- Produces: a terminal SSE `data:` line `{"type":"usage","input_tokens":N,"output_tokens":M}` after the answer stream (when usage is available). `_extract_usage(event) -> dict | None`.

- [ ] **Step 1: Verify the Strands usage shape.** Read the installed Strands SDK (`agent/_deps/strands/…` or wherever `Agent`/`stream_async` lives) to find where `stream_async` exposes token usage — typically a final event carrying an `AgentResult` with `.metrics.accumulated_usage` (`{"inputTokens","outputTokens","totalTokens"}`), or an event dict with a `usage`/`metadata.usage` field. Note the exact path; `_extract_usage` must handle it AND return `None` for events without it.

- [ ] **Step 2: Write the failing test.** Create `tests/unit/agent/test_extract_usage.py`. Because importing `agent/server.py` pulls heavy deps, import ONLY the helper via importlib from the file, and clean `agent/__pycache__` in teardown:

```python
import importlib.util, shutil
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[3] / "agent"
_SERVER = _AGENT / "server.py"


def _load():
    spec = importlib.util.spec_from_file_location("agent_server_for_test", _SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def teardown_module(_):
    # Agent Runtime deploy rejects a __pycache__ under agent/ — clean it.
    pc = _AGENT / "__pycache__"
    if pc.exists():
        shutil.rmtree(pc, ignore_errors=True)


def test_extract_usage_present():
    h = _load()
    # adapt this event to the real Strands shape found in Step 1:
    event = {"result": type("R", (), {"metrics": type("M", (), {
        "accumulated_usage": {"inputTokens": 120, "outputTokens": 340}})()})()}
    u = h._extract_usage(event)
    assert u == {"input_tokens": 120, "output_tokens": 340}


def test_extract_usage_absent_returns_none():
    h = _load()
    assert h._extract_usage({"data": "hello"}) is None


def test_extract_usage_malformed_no_raise():
    h = _load()
    assert h._extract_usage({"result": "weird"}) is None
    assert h._extract_usage(None) is None
```

If Step 1 shows a different real shape, adjust the `test_extract_usage_present` event AND `_extract_usage` to match — keep the absent/malformed cases.

Run: `python -m pytest tests/unit/agent/test_extract_usage.py -q` → FAIL (helper missing). Then confirm `agent/__pycache__` is gone after the run.

- [ ] **Step 3: Implement `_extract_usage` + emit.** In `agent/server.py`, add the helper (module level, near the top) — adapt the body to the shape confirmed in Step 1:

```python
def _extract_usage(event):
    """Pull {input_tokens, output_tokens} from a Strands stream event, or None.
    Fully defensive — never raises (returns None on any unexpected shape)."""
    try:
        result = event.get("result") if isinstance(event, dict) else None
        usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None)
        if not usage:
            return None
        inp = usage.get("inputTokens")
        out = usage.get("outputTokens")
        if inp is None and out is None:
            return None
        return {"input_tokens": int(inp or 0), "output_tokens": int(out or 0)}
    except Exception:
        return None
```

Then in the stream loop (currently `async for event in agent.stream_async(prompt): if ...: yield event["data"]`), capture usage and emit it at the end, all fail-safe:

```python
        last_usage = None
        async for event in agent.stream_async(prompt):
            if isinstance(event, dict) and "data" in event and isinstance(event["data"], str):
                yield event["data"]
            try:
                u = _extract_usage(event)
                if u:
                    last_usage = u
            except Exception:
                pass
        if last_usage:
            try:
                yield json.dumps({"type": "usage", **last_usage})
            except Exception:
                pass
```

Confirm `import json` is present at the top of `agent/server.py` (add it if not).

- [ ] **Step 4: Run the test + validate agent code.** `python -m pytest tests/unit/agent/test_extract_usage.py -q` → PASS. Then validate the file parses and clean pycache:

Run: `python -c "import ast,pathlib; ast.parse(pathlib.Path('agent/server.py').read_text())" && rm -rf agent/__pycache__ && echo OK`
Expected: `OK` (parses; no leftover pycache).

- [ ] **Step 5: Commit.** (Confirm `git status` shows no `agent/__pycache__`.)

```bash
git add agent/server.py tests/unit/agent/test_extract_usage.py
git commit -m "feat(token-usage): agent emits terminal usage SSE event (fail-safe)"
```

---

### Task 3: Frontend capture + session persistence + handler fields

**Files:**

- Modify: `frontend/src/lib/agentcore-sse.ts` (`streamChat` gains an `onUsage` callback + a `type === "usage"` branch)
- Modify: `frontend/src/components/chat/chat-panel.tsx` (accumulate per-session tokens/turns/error; include in the session PUT)
- Modify: `api/chat_sessions/handler.py` (PUT persists the 4 additive fields; list projection adds token totals)
- Test: `tests/unit/api/test_chat_sessions.py` (extend)

**Interfaces:**

- Consumes: the agent `{"type":"usage","input_tokens","output_tokens"}` SSE event (Task 2).
- Produces: `chat_sessions` records carrying optional `total_input_tokens`, `total_output_tokens`, `turn_count`, `last_error`.

- [ ] **Step 1: Add `onUsage` to `streamChat`.** In `frontend/src/lib/agentcore-sse.ts`, add an optional `onUsage?: (u: { input: number; output: number }) => void` parameter to `streamChat` (after `onError`). In the parse loop where `parsed.type` is dispatched, add:

```typescript
              } else if (parsed.type === "usage") {
                onUsage?.({
                  input: Number(parsed.input_tokens) || 0,
                  output: Number(parsed.output_tokens) || 0,
                });
```

(Place it alongside the existing `parsed.type === "tool_use"` branch; do not disturb the text/`content_block_delta`/`data` branches.)

- [ ] **Step 2: Accumulate + persist in the chat component.** In `frontend/src/components/chat/chat-panel.tsx`, find the `streamChat(...)` call and the place it persists the session (the `chat_sessions` PUT — search the file for the session-save call / `updateChatSession`/`putChatSession` in api-client). Maintain per-session running totals in component state (or a ref): on `onUsage`, add `input`→`totalInputTokens`, `output`→`totalOutputTokens`, increment `turnCount`; in `onError`, set `lastError = { message, at: Date.now() }`. When persisting the session, include `total_input_tokens`, `total_output_tokens`, `turn_count`, and `last_error` in the PUT body (extend the api-client save function's payload type to carry them, optional). Read the existing save call and thread these through without changing unrelated fields.

- [ ] **Step 3: Persist fields in the handler (write the failing test first).** Extend `tests/unit/api/test_chat_sessions.py` (read it first for its helpers). Add a test that a PUT body including the four fields stores them and that the list projection includes the token totals:

```python
def test_put_persists_token_fields(...):
    # PUT a session body with total_input_tokens/total_output_tokens/turn_count/last_error
    # assert the stored item carries them.
    ...
def test_put_without_token_fields_is_unchanged(...):
    # PUT without the fields → item has no token keys (additive, backward-compatible).
    ...
```

(Match the file's existing event/handler harness + table mock.)

Run: `python -m pytest tests/unit/api/test_chat_sessions.py -q` → new tests FAIL.

- [ ] **Step 4: Implement the handler fields.** In `api/chat_sessions/handler.py`, in the PUT path where `item = {...}` is built (~line 200), read the four optional fields from the parsed body and include them only when present:

```python
    for k in ("total_input_tokens", "total_output_tokens", "turn_count"):
        v = body.get(k)
        if isinstance(v, (int, float)):
            item[k] = int(v)
    if isinstance(body.get("last_error"), dict):
        item["last_error"] = body["last_error"]
```

And in the list path (`ProjectionExpression="session_id, title, cluster_id, updated_at, message_count, created_at"`, ~line 160), append `, total_input_tokens, total_output_tokens`.

Run: `python -m pytest tests/unit/api/test_chat_sessions.py -q` → PASS; then `python -m pytest tests/unit/api -q` → no regression.

- [ ] **Step 5: Build the frontend.** `cd frontend && npm run build` → PASS.

- [ ] **Step 6: Commit.**

```bash
git add frontend/src/lib/agentcore-sse.ts frontend/src/components/chat/chat-panel.tsx api/chat_sessions/handler.py frontend/src/lib/api-client.ts tests/unit/api/test_chat_sessions.py
git commit -m "feat(token-usage): capture per-session tokens + last error, persist on session"
```

---

### Task 4: Per-session surfacing UI — token total + error badge in the session list

**Files:**

- Modify: `frontend/src/components/chat/chat-panel.tsx` (or the session-list sub-component it renders)

**Interfaces:**

- Consumes: `total_input_tokens`/`total_output_tokens`/`last_error` on the session objects returned by the `chat_sessions` list (Task 3).

- [ ] **Step 1: Render per-session tokens + error.** In the chat session list/sidebar (find where `chat-panel.tsx` maps over sessions to render the list rows), add: a small token total `(total_input_tokens + total_output_tokens)` rendered with comma formatting (e.g. `1,234 tok`) when present, and an error badge (a small red dot/icon) when `last_error` is set, with the error message shown on hover (`title=`) or in an expandable detail. Korean label for the token hint (e.g. "토큰"). Use the existing design-system primitives + tokens; keep it visually consistent with the current session row (do not redesign the row). Guard against undefined (older sessions lack the fields → render nothing).

- [ ] **Step 2: Build the frontend.** `cd frontend && npm run build` → PASS.

- [ ] **Step 3: Commit.**

```bash
git add frontend/src/components/chat/chat-panel.tsx
git commit -m "feat(token-usage): show per-session token total + error badge in session list"
```

---

## Post-implementation (controller, after all tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD`.
- Deploy dev: `cdk deploy dbops-dev-agent` (cost Lambda CloudWatch IAM + the AgentCore Runtime if the agent code change is packaged through it — confirm how `agent/` deploys; the agent container/runtime update takes ~10 min to reach a warm container). Then frontend build → `aws s3 sync frontend/out/ s3://dbops-dev-frontend-830858425797 --delete --exclude config.json` → CloudFront invalidation `E3AHIXF7WMTX01`.
- Live smoke (viewer e2e token): `GET /api/cost?view=tokens` → 200 with `by_model`/`daily`/`note` keys (possibly empty); default `GET /api/cost` unchanged. Per-session token capture (agent usage event → session field) needs an interactive chat turn after the agent warm-container refresh — verify in the browser or document the live gap honestly.
- Then `superpowers:finishing-a-development-branch`.
