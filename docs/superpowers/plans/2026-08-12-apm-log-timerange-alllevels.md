# APM Log Search — All-Levels + Flexible Time Range — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let APM log search return ALL levels (opt-in) and accept a flexible time window — absolute start/end epoch, relative minutes, or hours — surfaced in the `/apm` UI with presets and a custom range.

**Architecture:** Extend the existing request-response `_logs_search` in `api/apm/handler.py` (Logs Insights query) — no streaming, no new infra. The frontend converts local datetime to epoch seconds; the backend only handles epoch seconds. Level filtering becomes opt-in-to-all: omitting `levels` keeps the ERROR+WARN default.

**Tech Stack:** Python 3.12 Lambda (boto3 CloudWatch Logs Insights), pytest, Next.js 16 static export (React, TypeScript).

## Global Constraints

- **Opt-in to all-levels.** `all: true` OR an explicit empty `levels: []` → no level filter. Omitting `levels` (absent/None) → ERROR+WARN default, unchanged. Distinguish absent from empty-list in the dispatcher.
- **Time window priority (epoch seconds):** `start`+`end` (both valid, start<end) > `minutes` (positive) > `hours` (1..48) > default 1h. Invalid inputs fall back to the next rule — never raise, never 500.
- **Max span 48h.** An absolute range wider than 48h is clamped (`start = end - 48*3600`), and the response sets `window_clamped: true`. Not rejected.
- **`limit` cap raised 500 → 2000.** Always applied (never unbounded).
- **CloudWatch Logs `start_query` takes EPOCH SECONDS** — no `* 1000`. All time values `< 10_000_000_000`.
- **Frontend:** this is a customized Next.js (see `frontend/AGENTS.md`); every page is `"use client"`. Do not add server-only code.
- **No change** to collector, cache schema, CDK, or OpenAPI (route path/method unchanged; only the POST body grows).

---

### Task 1: Backend — `_levels_filter` opt-in-to-all

**Files:**
- Modify: `api/apm/handler.py` (`_levels_filter`, ~line 225)
- Test: `tests/unit/api/test_apm_logs_search.py`

**Interfaces:**
- Produces: `_levels_filter(levels, all_levels=False) -> str`. Returns `""` (empty, no filter clause) when `all_levels` is True OR `levels` is an explicit empty list `[]`. Returns the ERROR+WARN default when `levels` is `None`/absent. Returns the sanitized OR-clause when `levels` is a non-empty list.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/api/test_apm_logs_search.py`:

```python
def test_levels_filter_all_via_flag_returns_empty():
    mod = _load()
    assert mod._levels_filter(None, all_levels=True) == ""


def test_levels_filter_explicit_empty_list_returns_empty():
    mod = _load()
    assert mod._levels_filter([], all_levels=False) == ""


def test_levels_filter_absent_still_defaults_error_warn():
    mod = _load()
    clause = mod._levels_filter(None)
    assert "ERROR" in clause and "WARN" in clause and "INFO" not in clause
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py -q`
Expected: FAIL (the two empty-returning cases fail; `_levels_filter` currently takes one arg and never returns "").

- [ ] **Step 3: Rewrite `_levels_filter`**

Replace the existing `_levels_filter` in `api/apm/handler.py` with:

```python
def _levels_filter(levels, all_levels=False):
    """Server-side level gate. Default ERROR+WARN. Opt in to ALL levels with
    all_levels=True or an explicit empty list []; then no filter is applied.
    `levels` absent/None keeps the ERROR+WARN default (never scans everything
    by accident)."""
    import re
    if all_levels or (isinstance(levels, list) and not levels):
        return ""
    lv = [re.sub(r"[^A-Z]", "", (x or "").upper()) for x in (levels or _DEFAULT_LEVELS)]
    lv = [x for x in lv if x] or _DEFAULT_LEVELS
    ors = " or ".join(f"@message like /{x}/" for x in lv)
    return f"filter ({ors})"
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py -q`
Expected: PASS (including the existing `test_levels_filter_defaults_to_error_warn` and `test_levels_filter_honors_explicit_levels`).

- [ ] **Step 5: Commit**

```bash
git add api/apm/handler.py tests/unit/api/test_apm_logs_search.py
git commit -m "feat(apm): _levels_filter supports opt-in all-levels (all flag / empty list)"
```

---

### Task 2: Backend — `_resolve_window` time-range helper

**Files:**
- Modify: `api/apm/handler.py` (add helper near `_logs_search`)
- Test: `tests/unit/api/test_apm_logs_search.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_resolve_window(body, now=None) -> (start_epoch:int, end_epoch:int, clamped:bool)`. All epoch seconds. Priority: valid `start`+`end` (start<end) > positive `minutes` > `hours` (1..48) > default 1h. Absolute span > 48h clamps `start` and returns `clamped=True`. `now` defaults to `int(time.time())` (injectable for tests).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/api/test_apm_logs_search.py`:

```python
def test_resolve_window_minutes():
    mod = _load()
    s, e, clamped = mod._resolve_window({"minutes": 5}, now=1_000_000)
    assert e == 1_000_000 and s == 1_000_000 - 300 and clamped is False


def test_resolve_window_absolute():
    mod = _load()
    s, e, clamped = mod._resolve_window({"start": 100, "end": 700}, now=1_000_000)
    assert s == 100 and e == 700 and clamped is False


def test_resolve_window_absolute_over_48h_clamps():
    mod = _load()
    span = 60 * 3600  # 60h
    s, e, clamped = mod._resolve_window({"start": 0, "end": span}, now=1_000_000)
    assert e == span and s == span - 48 * 3600 and clamped is True


def test_resolve_window_bad_absolute_falls_back_to_hours():
    mod = _load()
    # start >= end is invalid -> fall through to hours
    s, e, clamped = mod._resolve_window({"start": 900, "end": 100, "hours": 2}, now=1_000_000)
    assert e == 1_000_000 and s == 1_000_000 - 2 * 3600 and clamped is False


def test_resolve_window_default_one_hour():
    mod = _load()
    s, e, clamped = mod._resolve_window({}, now=1_000_000)
    assert e == 1_000_000 and s == 1_000_000 - 3600 and clamped is False


def test_resolve_window_hours_clamped_1_to_48():
    mod = _load()
    s, _, _ = mod._resolve_window({"hours": 999}, now=1_000_000)
    assert s == 1_000_000 - 48 * 3600
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py -q`
Expected: FAIL (`_resolve_window` not defined).

- [ ] **Step 3: Add `_resolve_window`**

Insert into `api/apm/handler.py` immediately before `def _logs_search`:

```python
_MAX_SPAN = 48 * 3600


def _resolve_window(body, now=None):
    """Return (start_epoch, end_epoch, clamped). Priority: absolute start+end
    (start<end) > relative minutes > relative hours (1..48) > default 1h.
    Any invalid value falls through to the next rule. Span capped at 48h."""
    import time as _time
    now = int(_time.time()) if now is None else int(now)

    def _int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    start, end = _int(body.get("start")), _int(body.get("end"))
    if start is not None and end is not None and start < end:
        if end - start > _MAX_SPAN:
            return end - _MAX_SPAN, end, True
        return start, end, False

    minutes = _int(body.get("minutes"))
    if minutes is not None and minutes > 0:
        minutes = min(minutes, _MAX_SPAN // 60)
        return now - minutes * 60, now, False

    hours = _int(body.get("hours"))
    if hours is not None:
        hours = max(1, min(48, hours))
        return now - hours * 3600, now, False

    return now - 3600, now, False
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/apm/handler.py tests/unit/api/test_apm_logs_search.py
git commit -m "feat(apm): _resolve_window resolves absolute/relative log time ranges"
```

---

### Task 3: Backend — wire window + all-levels + limit into `_logs_search`

**Files:**
- Modify: `api/apm/handler.py` (`_logs_search`)
- Test: `tests/unit/api/test_apm_logs_search.py`

**Interfaces:**
- Consumes: `_levels_filter(levels, all_levels)` (Task 1), `_resolve_window(body, now)` (Task 2).
- Produces: `_logs_search` uses the resolved window for `startTime`/`endTime`, applies `all_levels`, caps `limit` at 2000, and returns `start`, `end`, and (when clamped) `window_clamped` in the response body.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/api/test_apm_logs_search.py`:

```python
def _fake_target_monkeypatch(mod, monkeypatch):
    monkeypatch.setattr(mod, "_get_target", lambda t: {
        "target_id": t, "team": "", "region": "ap-northeast-2",
        "spoke_role_arn": "", "log_groups": ["/app/orders"]})


def test_logs_search_all_levels_has_no_level_filter(monkeypatch):
    mod = _load()
    _fake_target_monkeypatch(mod, monkeypatch)
    captured = {}

    class FakeLogs:
        def start_query(self, **kw):
            captured.update(kw)
            return {"queryId": "q1"}
        def get_query_results(self, **kw):
            return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    resp = mod._logs_search(_event({"log_group": "/app/orders", "all": True}), "svc-a")
    assert resp["statusCode"] == 200
    qs = captured["queryString"]
    assert "@message like /ERROR/" not in qs
    assert "fields @timestamp, @message" in qs and "sort @timestamp desc" in qs


def test_logs_search_limit_capped_at_2000(monkeypatch):
    mod = _load()
    _fake_target_monkeypatch(mod, monkeypatch)
    captured = {}

    class FakeLogs:
        def start_query(self, **kw):
            captured.update(kw); return {"queryId": "q1"}
        def get_query_results(self, **kw):
            return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    mod._logs_search(_event({"log_group": "/app/orders", "limit": 999999}), "svc-a")
    assert "limit 2000" in captured["queryString"]


def test_logs_search_minutes_window(monkeypatch):
    mod = _load()
    _fake_target_monkeypatch(mod, monkeypatch)
    captured = {}

    class FakeLogs:
        def start_query(self, **kw):
            captured.update(kw); return {"queryId": "q1"}
        def get_query_results(self, **kw):
            return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    mod._logs_search(_event({"log_group": "/app/orders", "minutes": 5}), "svc-a")
    span = captured["endTime"] - captured["startTime"]
    assert span == 300
    assert captured["startTime"] < 10_000_000_000  # epoch seconds


def test_logs_search_reports_clamp(monkeypatch):
    mod = _load()
    _fake_target_monkeypatch(mod, monkeypatch)

    class FakeLogs:
        def start_query(self, **kw): return {"queryId": "q1"}
        def get_query_results(self, **kw): return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    resp = mod._logs_search(_event({"log_group": "/app/orders", "start": 0, "end": 60 * 3600}), "svc-a")
    body = json.loads(resp["body"])
    assert body.get("window_clamped") is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py -q`
Expected: FAIL (all-levels still injects ERROR filter; limit cap is 500; no `window_clamped`).

- [ ] **Step 3: Rewrite the body of `_logs_search`**

In `api/apm/handler.py`, replace the block from `body = json.loads(...)` down to the `query_string = (...)` assignment (the `hours`/`limit`/`parts`/`query_string` section) with:

```python
    body = json.loads(event.get("body") or "{}")
    log_group = body.get("log_group") or (item.get("log_groups") or [""])[0]
    if not log_group:
        return _resp(400, {"error": "no log_group for target"})

    start_epoch, end_epoch, clamped = _resolve_window(body)
    try:
        limit = min(max(1, int(body.get("limit", 100) or 100)), 2000)
    except (ValueError, TypeError):
        limit = 100

    parts = []
    lf = _levels_filter(body.get("levels"), all_levels=bool(body.get("all")))
    if lf:
        parts.append(lf)
    for raw in (body.get("query") or "").split():
        cleaned = re.sub(r"[^A-Za-z0-9_./:\-]", "", raw)
        if cleaned:
            parts.append(f"filter @message like /{cleaned}/")
    query_string = ("fields @timestamp, @message"
                    + ("".join(" | " + p for p in parts))
                    + f" | sort @timestamp desc | limit {limit}")
```

Then update the `base` dict and the `start_query` call. Change `base` to include the window:

```python
    client = _logs_client_for(item)
    base = {"target_id": target_id, "log_group": log_group,
            "compiled_query": query_string, "start": start_epoch, "end": end_epoch,
            "entries": [], "count": 0}
    if clamped:
        base["window_clamped"] = True
    try:
        qid = client.start_query(
            logGroupName=log_group,
            startTime=start_epoch,
            endTime=end_epoch,
            queryString=query_string)["queryId"]
```

Leave the poll loop and error/timeout handling below unchanged.

- [ ] **Step 4: Run to verify all pass**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py -q`
Expected: PASS (new tests + the original `test_logs_search_runs_query`, which sends no `levels`/`all` so still asserts the ERROR default and epoch-seconds).

- [ ] **Step 5: Run the full APM suite for no regressions**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py tests/unit/api/test_apm_reads.py tests/unit/api/test_apm_handler.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add api/apm/handler.py tests/unit/api/test_apm_logs_search.py
git commit -m "feat(apm): logs_search honors time window, all-levels, limit 2000"
```

---

### Task 4: Frontend — `searchApmLogs` params

**Files:**
- Modify: `frontend/src/lib/api-client.ts` (`searchApmLogs`, ~line 3385)

**Interfaces:**
- Produces: `searchApmLogs(id, opts)` where `opts` gains `all?`, `minutes?`, `start?`, `end?` alongside the existing `levels?`, `query?`, `hours?`, `limit?`, `log_group?`.

- [ ] **Step 1: Widen the opts type**

Replace the `searchApmLogs` signature's `opts` type in `frontend/src/lib/api-client.ts`:

```ts
export async function searchApmLogs(
  id: string,
  opts: {
    levels?: string[];
    all?: boolean;
    query?: string;
    minutes?: number;
    hours?: number;
    start?: number;
    end?: number;
    limit?: number;
    log_group?: string;
  },
) {
  const res = await authedFetch(await api(`/api/apm/targets/${enc(id)}/logs/search`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(opts),
  });
  if (!res.ok) throw new Error(`APM 로그 검색 실패 (상태 ${res.status})`);
  return res.json();
}
```

(The body already serializes only the keys present, so no other change is needed.)

- [ ] **Step 2: Commit**

```bash
git add frontend/src/lib/api-client.ts
git commit -m "feat(apm): searchApmLogs accepts all/minutes/start/end params"
```

---

### Task 5: Frontend — time-range selector + all-levels toggle on `/apm`

**Files:**
- Modify: `frontend/src/app/apm/page.tsx`

**Interfaces:**
- Consumes: `searchApmLogs(id, {levels, all, query, minutes, start, end, limit})` (Task 4).

- [ ] **Step 1: Add state + resolved-params logic**

In `frontend/src/app/apm/page.tsx`, after the existing `const [query, setQuery] = useState("");` (line ~32) add:

```tsx
  const [allLevels, setAllLevels] = useState(false);
  // Relative preset in minutes; 0 means "custom range" (use start/end below).
  const [rangeMin, setRangeMin] = useState<number>(60);
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
```

- [ ] **Step 2: Rewrite `runSearch` to send the resolved window + all-levels**

Replace the existing `runSearch` (lines ~54-62) with:

```tsx
  const runSearch = useCallback(() => {
    if (!selected) return;
    setSearching(true);
    setError(null);
    const opts: {
      levels?: string[]; all?: boolean; query: string; limit: number;
      minutes?: number; start?: number; end?: number;
    } = { query, limit: 2000 };
    if (allLevels) opts.all = true;
    else opts.levels = levels;
    if (rangeMin === 0) {
      // Custom range: convert local datetime-local values to epoch seconds.
      if (customStart) opts.start = Math.floor(new Date(customStart).getTime() / 1000);
      if (customEnd) opts.end = Math.floor(new Date(customEnd).getTime() / 1000);
    } else {
      opts.minutes = rangeMin;
    }
    searchApmLogs(selected, opts)
      .then((r) => setLogs(r.entries || []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSearching(false));
  }, [selected, levels, allLevels, query, rangeMin, customStart, customEnd]);
```

- [ ] **Step 3: Add the time-range + all-levels controls to the "로그 검색" Section**

In the `<Section title="로그 검색">` block, immediately after the opening
`<div className="flex flex-wrap items-center gap-2 mb-3">` and BEFORE the
`{LEVELS.map(...)}` toggles, insert the time-range controls:

```tsx
              {/* Time range presets + custom */}
              {[
                { label: "5분", m: 5 },
                { label: "10분", m: 10 },
                { label: "30분", m: 30 },
                { label: "1시간", m: 60 },
                { label: "6시간", m: 360 },
                { label: "사용자 지정", m: 0 },
              ].map((r) => (
                <button
                  key={r.label}
                  onClick={() => setRangeMin(r.m)}
                  className={`text-xs px-2 py-1 rounded border ${
                    rangeMin === r.m
                      ? "bg-emerald-900/40 border-emerald-500 text-emerald-300"
                      : "bg-zinc-900 border-zinc-700 text-zinc-500"
                  }`}
                >
                  {r.label}
                </button>
              ))}
              {rangeMin === 0 && (
                <>
                  <input
                    type="datetime-local"
                    value={customStart}
                    onChange={(e) => setCustomStart(e.target.value)}
                    className="text-xs bg-zinc-900 border border-zinc-700 rounded px-2 py-1"
                  />
                  <span className="text-xs text-zinc-500">~</span>
                  <input
                    type="datetime-local"
                    value={customEnd}
                    onChange={(e) => setCustomEnd(e.target.value)}
                    className="text-xs bg-zinc-900 border border-zinc-700 rounded px-2 py-1"
                  />
                </>
              )}
              {/* All-levels toggle */}
              <button
                onClick={() => setAllLevels((v) => !v)}
                className={`text-xs px-2 py-1 rounded border ${
                  allLevels
                    ? "bg-emerald-900/40 border-emerald-500 text-emerald-300"
                    : "bg-zinc-900 border-zinc-700 text-zinc-500"
                }`}
              >
                전체 레벨
              </button>
```

Then change the existing per-level `{LEVELS.map((lv) => ...)}` buttons so they
are disabled/dimmed when `allLevels` is on. Replace the `<button ...>` inside
that map with:

```tsx
                <button
                  key={lv}
                  onClick={() => toggleLevel(lv)}
                  disabled={allLevels}
                  className={`text-xs px-2 py-1 rounded border ${
                    allLevels
                      ? "bg-zinc-900 border-zinc-800 text-zinc-600 cursor-not-allowed"
                      : levels.includes(lv)
                      ? "bg-zinc-700 border-zinc-500"
                      : "bg-zinc-900 border-zinc-700 text-zinc-500"
                  }`}
                >
                  {lv}
                </button>
```

- [ ] **Step 4: Type-check / build**

Run: `cd frontend && npm run build`
Expected: build succeeds, `/apm` compiles with no TypeScript errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/apm/page.tsx
git commit -m "feat(apm): /apm time-range presets + custom range + all-levels toggle"
```

---

### Task 6: Full verification

**Files:** none (verification task)

- [ ] **Step 1: Backend suite**

Run: `python3 -m pytest tests/unit/api/test_apm_logs_search.py tests/unit/api/test_apm_reads.py tests/unit/api/test_apm_handler.py tests/unit/api/test_tenancy_parity.py -q`
Expected: PASS.

- [ ] **Step 2: Frontend build**

Run: `cd frontend && npm run build`
Expected: build succeeds; `/apm` route emitted.

- [ ] **Step 3: (If a live env is available) browser/API smoke**

Optional, human-gated: against a deployed env, `POST /api/apm/targets/<id>/logs/search` with `{"all": true, "minutes": 10}` returns 200 with `compiled_query` lacking an ERROR/WARN filter and a `start`/`end` ~600s apart. Skip if no live env.

---

## Self-Review

**Spec coverage:**
- All-levels via `all:true` / `levels:[]`, omit = ERROR+WARN → Task 1 ✓
- Time priority start/end > minutes > hours > 1h → Task 2 ✓
- 48h clamp + `window_clamped` → Tasks 2, 3 ✓
- `limit` cap 2000 → Task 3 ✓
- epoch seconds, no `* 1000` → Tasks 2, 3 (tests assert `< 10_000_000_000`) ✓
- Frontend presets (5/10/30/60/360 min) + custom datetime → epoch → Task 5 ✓
- All-levels toggle disables level buttons → Task 5 ✓
- api-client params → Task 4 ✓
- No collector/schema/CDK/OpenAPI change → nothing touches them ✓

**Placeholder scan:** No TBD/TODO; every code step has real code. ✓

**Type consistency:** `_levels_filter(levels, all_levels=False)`, `_resolve_window(body, now=None)→(start,end,clamped)` consistent across Tasks 1-3. Frontend `all/minutes/start/end` consistent Tasks 4-5. `rangeMin===0` sentinel = custom range, consistent within Task 5. ✓

**Note for implementer:** run tasks in order — Task 3 depends on the Task 1/2 signatures. `frontend/AGENTS.md` warns this is a customized Next.js; the page edits are plain client-component React (state + fetch), no framework APIs, so standard usage applies.
</content>
