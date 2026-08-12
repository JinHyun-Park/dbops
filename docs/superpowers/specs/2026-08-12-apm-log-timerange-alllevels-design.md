# APM Log Search — All-Levels + Flexible Time Range — Design

**Date:** 2026-08-12
**Status:** Approved (brainstorming), pending implementation plan
**Scope:** Extend the APM on-demand log search so a user can (1) see ALL logs
regardless of level, not just ERROR+WARN, and (2) pick the time window —
relative presets (5/10/30 min, 1/6 h) or an explicit start/end range.

## 1. Goal & Non-Goals

### Goal

Today `POST /api/apm/targets/{id}/logs/search` always forces an ERROR+WARN
level filter and only accepts `hours` (1–48). Extend it so the caller can:

- Request **all levels** (INFO/DEBUG included) for a window, explicitly.
- Choose the window as **relative minutes**, relative hours (kept), or an
  **absolute start/end** epoch-second range.
- Surface the controls in the `/apm` page UI.

### Non-Goals (YAGNI)

- No real-time streaming / WebSocket tail. This stays request-response
  (Logs Insights query). A future polling loop can reuse this API unchanged.
- No timezone handling in the backend — the frontend converts the user's local
  datetime to epoch seconds; the backend only ever deals in epoch seconds.
- No change to the collector, cache tables, or metrics paths.

## 2. Key Decisions (from brainstorming)

| Decision | Choice |
| --- | --- |
| Exposure model | All-levels + time range, request-response (not streaming) |
| "All levels" request shape | `all: true` OR an explicit empty `levels: []`. **Omitting `levels` keeps the ERROR+WARN default** — you must opt in to all-levels, so nothing scans everything by accident. |
| Time range inputs | `start`+`end` (epoch seconds) > `minutes` > `hours` (existing) > default 1h |
| Absolute time source | Frontend converts local datetime → epoch seconds (UTC). Backend takes epoch seconds only. |
| Max span | 48h retained; an absolute range wider than 48h is clamped (not rejected). |
| `limit` cap | Raised 500 → 2000 (all-levels returns more rows). |
| Applied to | Backend + `/apm` page UI. |

## 3. Backend — `api/apm/handler.py`

### `_levels_filter(levels, all_levels=False)`

- If `all_levels` is true, OR `levels` is an explicit empty list `[]` →
  return `""` (no filter clause; the query returns every level).
- Else if `levels` is a non-empty list → sanitize (`[^A-Z]` stripped, upper)
  and OR them, as today.
- Else (`levels` is `None`/absent) → the ERROR+WARN default, unchanged.

The dispatcher must distinguish "absent" from "empty list": read
`body.get("levels")` and pass an explicit sentinel so `[]` is not collapsed to
`None`. Pass `all_levels=bool(body.get("all"))`.

### Time window resolution (new helper `_resolve_window(body)` → `(start_epoch, end_epoch)`)

Priority, all in **epoch seconds**:

1. `start` and `end` both present and valid ints with `start < end` →
   use them; if `end - start > 48*3600`, clamp `start = end - 48*3600` and set
   `window_clamped: true` in the response.
2. else `minutes` present, positive int → `end = now`, `start = now - minutes*60`
   (clamp minutes to ≤ 48*60).
3. else `hours` present → `end = now`, `start = now - hours*3600`
   (clamp 1..48, existing behavior).
4. else default → last 1 hour.

Invalid/non-numeric inputs fall back to the next rule (never raise → never 500).
`now = int(time.time())`.

### `_logs_search` assembly

- `parts = []`; `lf = _levels_filter(...)`; if `lf`: `parts.append(lf)`.
- free-text `query` terms: sanitized `@message like /term/` appended (as today),
  works in all-levels mode too.
- `query_string = "fields @timestamp, @message" + (" | " + " | ".join(parts) if parts else "") + f" | sort @timestamp desc | limit {limit}"`
  — when there are no filter parts (all-levels, no text) the query is just
  fields + sort + limit.
- `limit = min(max(1, int(body.get("limit", 100) or 100)), 2000)` (cap 2000).
- `start_query(logGroupName=..., startTime=start_epoch, endTime=end_epoch, queryString=...)`
  — epoch seconds (already fixed; no `* 1000`).
- Response includes `compiled_query`, `start`, `end`, and `window_clamped` when set.

## 4. Frontend — `frontend/src/app/apm/page.tsx`

- **Time range selector**: preset buttons **5분 / 10분 / 30분 / 1시간 / 6시간**
  (each sets a `minutes` value), plus a **"사용자 지정"** toggle revealing two
  `datetime-local` inputs (start, end).
- On custom range: convert each local value with
  `Math.floor(new Date(v).getTime() / 1000)` → `start` / `end` epoch seconds.
- **Level "전체" toggle**: when on, disable the individual ERROR/WARN/INFO/DEBUG
  toggles and send `all: true`; when off, send the selected `levels` as today
  (default still ERROR+WARN).
- Pass the resolved params to `searchApmLogs`. Show `window_clamped` as a small
  notice if the backend clamped a >48h custom range.

## 5. API Client — `frontend/src/lib/api-client.ts`

`searchApmLogs(id, opts)` where `opts` gains optional fields:
`{ levels?: string[]; all?: boolean; query?: string; minutes?: number; hours?: number; start?: number; end?: number; limit?: number; log_group?: string }`.
Body is `JSON.stringify(opts)` (only set keys are sent).

## 6. Testing — `tests/unit/api/test_apm_logs_search.py`

- `_levels_filter(None)` → contains ERROR and WARN (regression).
- `_levels_filter([], all_levels=False)` → `""` (explicit empty = all).
- `_levels_filter(None, all_levels=True)` → `""`.
- `_logs_search` with `all: true` → `compiled_query` has NO `@message like /ERROR/`
  filter clause; still has `fields`, `sort`, `limit`.
- Window: `minutes: 5` → captured `startTime` ≈ `endTime - 300` (± small).
- Window: `start`/`end` absolute → passed through; `start >= end` → falls back.
- Window: absolute span > 48h → clamped to 48h, `window_clamped: true`.
- Window: none of start/end/minutes/hours → ~1h.
- `limit` capped at 2000 (e.g. request 999999 → 2000).
- All values are epoch SECONDS (`startTime < 10_000_000_000`), no regression.

## 7. Files Touched

- **Modified:** `api/apm/handler.py` (`_levels_filter`, new `_resolve_window`,
  `_logs_search`), `tests/unit/api/test_apm_logs_search.py`,
  `frontend/src/app/apm/page.tsx`, `frontend/src/lib/api-client.ts`.
- **No change:** collector, cache schema, CDK, OpenAPI (route path/method
  unchanged — only the POST body grows, which the generated spec does not model).
</content>
