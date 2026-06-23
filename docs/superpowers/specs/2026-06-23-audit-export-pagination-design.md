# Audit Export — Unbounded (Cursor Pagination) — Design

**Date:** 2026-06-23
**Status:** approved

## Problem

The `/activity` audit export caps at 500 rows. `GET /api/activity`
(`api/approvals/handler.py`) reads the whole approvals table via `_scan_all`,
sorts by `created_at` desc, then truncates to `limit` (hard-capped at 500). The
client export re-fetches `limit=500` and marks the file `-capped` when it hits
the ceiling. So a compliance export of a filter that matches more than 500
approvals silently drops the older ones.

## Goal

Let the audit export retrieve **all** matching approval rows, regardless of
count, with no new infrastructure and no change to the existing timeline UI
(which keeps its sorted, ≤500 view). Approvals are TTL'd/short-lived so the
realistic table is small, but the export must not silently truncate.

Non-goals: exporting from the S3/Iceberg archive (separate, larger scope);
server-side CSV generation / S3 presigned download (cursor pagination needs no
infra and is unbounded — revisit only if response-assembly on the client
becomes a problem).

## Architecture

Add an opt-in **cursor pagination mode** to `GET /api/activity`. The export
client loops pages until the cursor is exhausted, accumulates rows, sorts them
client-side, and builds the CSV with the existing `buildAuditCsv`. The default
(no-cursor) request path is unchanged.

### Components

1. **Backend — `api/approvals/handler.py` `/api/activity`**

   - New optional query params: `cursor` (an opaque base64 token) and the
     existing `limit` reinterpreted in cursor mode as the per-page size
     (default 500, max 1000). Filters (`cluster_id`, `actor`, `action_type`)
     work identically in both modes.
   - **Mode switch:** a request is in **paginated mode** when `cursor` is
     present OR `export=true` is set. Otherwise the existing behavior is
     unchanged (full `_scan_all` → sort desc → truncate to `min(limit,500)` →
     `{items, count}`).
   - **Paginated mode:** do a SINGLE `table.scan(**filters, Limit=<page>,
ExclusiveStartKey=<decoded cursor>)` — one DDB page, NOT `_scan_all` (which
     would exhaust). Return the page's rows (same `compact` projection as today,
     unsorted — global sort happens client-side once all pages are in) plus
     `next_cursor`: a base64-encoded JSON of the scan's `LastEvaluatedKey`, or
     `null` when the scan is exhausted.
   - Response shape (paginated): `{"items": [...], "count": <page len>,
"next_cursor": "<b64>" | null}`. The default mode keeps `{"items", "count"}`
     (adding a `next_cursor: null` there is harmless and acceptable).
   - **Cursor codec:** `next_cursor = base64(json.dumps(LastEvaluatedKey))`;
     decode reverses it. A malformed/undecodable `cursor` → `400`
     (`{"error": "invalid cursor"}`), not a 500.

2. **Frontend — `frontend/src/lib/api-client.ts`**

   - Extend `fetchActivity` opts with `cursor?: string` and `export?: boolean`;
     thread them into the query string. Return type gains
     `next_cursor?: string | null`.
   - Add `fetchAllActivity(opts)` helper: loop `fetchActivity({...opts,
export: true, cursor})` accumulating `items` until `next_cursor` is null,
     with a **hard page ceiling** (e.g. 200 pages) to bound a runaway loop;
     if the ceiling is hit, stop and flag `capped: true`. Returns
     `{items: ActivityItem[], capped: boolean}`.

3. **Frontend — `frontend/src/app/activity/page.tsx`**
   - The export button's onClick calls `fetchAllActivity({...filters})` instead
     of the single `limit=500` fetch, sorts the accumulated rows by `created_at`
     desc client-side, builds the CSV with `buildAuditCsv`, and names the file
     `audit-<date>-<N>rows.csv` (drop `-capped` unless the page ceiling was
     actually hit, in which case keep an explicit marker).

## Data Flow

- **Timeline view (unchanged):** `GET /api/activity?limit=200` → full scan →
  sorted → top 200 → rendered.
- **Export:** click → `fetchAllActivity` loops `GET /api/activity?export=true&
cursor=…` page by page → client accumulates all rows → sorts desc → CSV →
  browser download.

## Error Handling

- Malformed/expired `cursor` → `400 invalid cursor`. The client loop treats a
  non-OK page as a hard error (surface to the user), not a silent partial file.
- Page-ceiling hit (pathological table size) → stop, mark the filename/UI as
  capped — honest, not silent.
- Empty result → a valid CSV with the header row only.

## Testing

- **Backend:** default mode unchanged (sorted, ≤500, `{items,count}`);
  paginated mode page 1 returns a `next_cursor` when `LastEvaluatedKey` is
  present and rows for that page; page 2 with that cursor returns the remainder
  and `next_cursor: null` at exhaustion; filters apply in paginated mode; a
  garbage `cursor` → `400`. (Mock `table.scan` to return a `LastEvaluatedKey`
  on the first call and none on the second.)
- **Frontend:** `npm run build` green. (`fetchAllActivity` loop logic is covered
  by the backend round-trip + manual/live smoke.)

## Security

- No new surface: same `/api/activity` route, same Cognito JWT authorizer, same
  read-only scan of the approvals table. The cursor is an opaque
  base64(LastEvaluatedKey) — it leaks only a DDB key the caller already has
  access to via the same endpoint. No data beyond the existing `compact`
  projection is exposed.
