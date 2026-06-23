# Audit Log Export (CSV) — Design + Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Add a client-side "CSV 내보내기" download to the /activity (audit) page — serialize the already-fetched audit rows to a CSV file, for compliance/offline review. Frontend-only; no backend/CDK/openapi.

**Architecture:** The /activity page already fetches `ActivityItem[]` via `fetchActivity` and renders them. Add a pure `buildAuditCsv(items)` helper + an export button that Blob-downloads it — mirroring the report-download (client-side) pattern already shipped.

**Tech Stack:** Next.js 16 client component, TypeScript.

## Global Constraints

- Frontend-only — no new endpoint/backend/CDK/openapi. Uses the rows already fetched by `fetchActivity`.
- **Proper CSV escaping** — fields (esp. `action_details_excerpt`) can contain commas/quotes/newlines; wrap every field in double-quotes and escape embedded `"` as `""`. No CSV-injection mitigation beyond quoting is required (internal compliance export), but prefix a leading `=`/`+`/`-`/`@` field with `'` is a nice-to-have — optional.
- Button disabled/hidden when there are no rows. Korean human-facing label ("CSV 내보내기"); CSV header keys English (column names).
- Reuse existing button styling — no new component/CSS tokens (a small `lib/` helper is fine).
- Commit: conventional subject; NO `Co-Authored-By: Claude` trailer; no internal-roadmap refs. Frontend prettier hook → `git add -A` + re-commit if it reformats.

## ActivityItem shape (CSV columns, in order)

`created_at, cluster_id, action_type, approval_status, requested_by, approved_by, resolved_at, consumed_at, approval_id, action_details_excerpt`
(from `ActivityItem` in api-client.ts:1872 — `approved_by`/`resolved_at`/`consumed_at` optional → empty string when absent.)

---

## Task 1: CSV helper + export button on /activity

**Files:** Create `frontend/src/lib/audit-export.ts`; Modify `frontend/src/app/activity/page.tsx`.

**Interfaces:** `buildAuditCsv(items: ActivityItem[]): string` (header + one row per item, fully quoted/escaped).

- [ ] **Step 1: Pure helper** `frontend/src/lib/audit-export.ts`:

```typescript
import type { ActivityItem } from "@/lib/api-client";

const COLS: (keyof ActivityItem)[] = [
  "created_at",
  "cluster_id",
  "action_type",
  "approval_status",
  "requested_by",
  "approved_by",
  "resolved_at",
  "consumed_at",
  "approval_id",
  "action_details_excerpt",
];

function csvCell(v: unknown): string {
  const s = v === undefined || v === null ? "" : String(v);
  return `"${s.replace(/"/g, '""')}"`; // always quote; escape embedded quotes
}

export function buildAuditCsv(items: ActivityItem[]): string {
  const header = COLS.join(",");
  const rows = items.map((it) => COLS.map((c) => csvCell(it[c])).join(","));
  return [header, ...rows].join("\r\n"); // CRLF for Excel friendliness
}
```

(If `frontend/` has a vitest/jest harness, add tests: header order, quoting of a field containing `,`/`"`/newline, missing optional → empty cell. If no harness, skip — build is the gate.)

- [ ] **Step 2: Export button** in `frontend/src/app/activity/page.tsx` — read the page's audit items state (the array passed to the rendered list, from `fetchActivity`). Add a "CSV 내보내기" button (near the page header/filters, reuse existing button styling). On click: `const csv = buildAuditCsv(items)` → `Blob([csv], {type:"text/csv;charset=utf-8"})` → temp `<a download="audit-${new Date().toISOString().slice(0,10)}.csv">` → click → `URL.revokeObjectURL`. Disable/hide the button when `items.length === 0`.

- [ ] **Step 3: Build** — `cd frontend && npm run build` → exit 0, `/activity` in route list.

- [ ] **Step 4: Commit (mind prettier)** — `git add frontend/src/lib/audit-export.ts frontend/src/app/activity/page.tsx` ; `git commit -m "feat(activity): client-side CSV export of the audit log"` (prettier reformat → `git add -A` + re-run).

## Self-Review

- Frontend-only, additive (existing /activity rendering untouched). ✓
- CSV escaping handles `,`/`"`/newline; missing optionals → empty. ✓
- Korean button label; column keys English. ✓
- Button gated on items present. ✓
