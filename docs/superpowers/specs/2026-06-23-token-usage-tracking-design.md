# Token Usage Tracking + Per-Session Error Surfacing — Design

**Date:** 2026-06-23
**Status:** approved

## Problem

DBOps surfaces Bedrock **dollar** cost (the `/cost` tab, via Cost Explorer) but
never **token usage**, and there is no per-session attribution of tokens or any
surfacing of which chat sessions errored. Token usage is not captured anywhere
in app code today (`agent/server.py` streams Strands events but discards the
usage metadata; the sessions table holds title/message_count/messages only).

## Goal

Surface token usage two ways: (A) a **fleet-aggregate** read-only view (tokens
by model + over time) from CloudWatch Bedrock metrics, no agent change; and (B)
**per-session** token totals + last error, captured by instrumenting the agent
stream and persisted on the existing session record. Capturing usage must NEVER
break the chat stream.

Non-goals: cost-per-token dollar math (the cost tab already does dollars);
historical backfill of token usage for past sessions; cross-account token
aggregation.

## Architecture

Two independent data paths converging in the UI:

- **(A) Fleet:** `GET /api/cost?view=tokens` reads CloudWatch `AWS/Bedrock`
  token metrics (read-only, mirrors the existing Cost-Explorer view pattern).
- **(B) Per-session:** the agent emits a terminal `usage` SSE event; the
  frontend accumulates it onto the session and persists it via the existing
  `chat_sessions` PUT; the session list surfaces it.

### Components

1. **Fleet token view — `api/cost/handler.py` + cost Lambda IAM**

   - Add `?view=tokens` to the existing `view` dispatch (alongside `rds`,
     `platform`, default `bedrock`). New `_handle_tokens_view(start, end, days)`:
     a CloudWatch client (`boto3.client("cloudwatch")`) discovers Bedrock model
     dimensions via `list_metrics(Namespace="AWS/Bedrock", MetricName="InputTokenCount")`
     then `get_metric_data` sums `InputTokenCount` + `OutputTokenCount` per
     `ModelId` over the range (daily period for the time series).
   - Response: `{"view": "tokens", "days", "by_model": [{"model", "input",
"output", "total"}], "daily": [{"date", "input", "output"}], "note": ...}`.
   - **Caveat (documented in the `note`):** CloudWatch Bedrock token metrics are
     not DBOps-tag-filterable, so this view is **account-wide Bedrock token
     usage by model** — same untagged-scope honesty the cost views already carry.
   - IAM: add `cloudwatch:GetMetricData` + `cloudwatch:ListMetrics` to the
     `CostApi` Lambda role (it currently has only `ce:*`).

2. **Fleet token UI — `frontend/src/app/cost/page.tsx` (+ api-client)**

   - Add a "토큰" view toggle (next to the existing Bedrock/RDS/Platform views).
     Render `by_model` (a bar/table) + `daily` (a time-series chart) reusing the
     existing chart primitives the cost page already uses.
   - `api-client.ts`: extend the cost fetch to pass `view=tokens` and type the
     token response.

3. **Agent usage emission — `agent/server.py`** (deployment-sensitive)

   - Factor a pure helper `_extract_usage(event) -> dict | None` that pulls
     `{input_tokens, output_tokens}` from a Strands `stream_async` event
     (the event carrying the `AgentResult`/accumulated usage — the implementer
     verifies the exact shape against the installed Strands SDK). Returns `None`
     when the event has no usage.
   - In the stream loop: accumulate the latest non-None usage; after the loop
     completes, `yield json.dumps({"type": "usage", "input_tokens", "output_tokens"})`
     so the runtime frames it as a terminal SSE `data:` line.
   - **FAIL-SAFE:** all usage extraction/emission is wrapped so any error is
     swallowed and the chat stream is unaffected — a missing/changed usage shape
     simply omits the marker. The chat answer must always stream normally.
   - Errors: a turn that raises already surfaces through the existing stream
     error path; no change needed beyond confirming the frontend can observe it
     (component 4 records it per-session).

4. **Frontend capture + persist — `frontend/src/lib/agentcore-sse.ts`, the chat
   component, `api/chat_sessions/handler.py`**

   - `agentcore-sse.ts`: the SSE parser already dispatches on `parsed.type`; add
     a `parsed.type === "usage"` branch → invoke a new optional `onUsage({input,
output})` callback (added to the stream function's params).
   - Chat component (the one that owns the SSE call + session persistence): on
     `onUsage`, add to running per-session totals; track `turn_count`; on a turn
     error, capture `last_error` (message + timestamp). Include
     `total_input_tokens`, `total_output_tokens`, `turn_count`, `last_error`
     in the existing `chat_sessions` PUT body.
   - `chat_sessions/handler.py`: the PUT persists these four additive fields
     when present (never required — old sessions and non-usage turns omit them);
     the list `ProjectionExpression` adds `total_input_tokens` +
     `total_output_tokens` so the session list can show totals without a full
     item read.

5. **Per-session surfacing UI — the chat session list/sidebar**
   - Show per-session token total (input+output) next to each session, and an
     error indicator (e.g. a small badge) when `last_error` is set, with the
     error text on hover/expand. Korean labels for any explanatory text.

## Data Flow

- **Fleet:** `/cost` "토큰" view → `GET /api/cost?view=tokens` → CloudWatch
  GetMetricData (AWS/Bedrock) → by-model + daily → charts.
- **Per-session:** chat turn → agent streams answer, then a `usage` SSE event →
  frontend accumulates onto the session → `chat_sessions` PUT persists totals +
  last_error → session list renders per-session tokens + error badge.

## Error Handling

- Agent: `_extract_usage` + the usage emit are fully fail-safe — never raise,
  never block the answer stream. No usage shape found → no marker (graceful).
- Fleet view: no CloudWatch data / metric absent → empty `by_model`/`daily`
  (valid empty view), not an error. A CloudWatch permission error → surfaced as
  the view's error string (mirrors the cost views).
- Session fields are all additive + optional — a session that never received a
  usage event simply has no token fields; the UI shows nothing for it.

## Testing

- **Fleet (Increment 1):** `_handle_tokens_view` unit tests with a mocked
  CloudWatch client — `list_metrics` discovery + `get_metric_data` aggregation
  into `by_model`/`daily`; empty-metrics → empty view; the `?view=tokens`
  dispatch routes correctly and other views are unchanged.
- **Agent (Increment 2):** `_extract_usage` unit tests over representative
  Strands event dicts (usage present → dict; usage absent → None; malformed →
  None, no raise). Validate `agent/server.py` with `ast.parse` (NOT import-exec
  in the deploy path); any test that imports agent code must leave no
  `__pycache__` in `agent/` at deploy time (the deploy step cleans it).
- **Per-session (Increment 3):** `chat_sessions` handler stores the four fields
  on PUT + returns token totals in the list projection; frontend `npm run build`.
- **UI (Increment 4):** frontend `npm run build`.

## Security

- Fleet view: read-only CloudWatch GetMetricData behind the existing Cognito
  authorizer; account-wide Bedrock token counts (no secrets, no PII).
- Per-session fields are token counts + an error string the user already saw in
  their own chat; stored on their own session record. No new authz surface
  (chat_sessions already enforces per-user session ownership).
- The agent change only emits aggregate token counts — no prompt/response
  content beyond what already streams.
