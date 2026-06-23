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
  let s = v === undefined || v === null ? "" : String(v);
  // CSV formula-injection guard: a cell starting with = + - @ (or tab/CR) can
  // execute as a formula when an auditor opens the file in Excel/Sheets. Prefix
  // a single quote to neutralize it (spreadsheets hide the leading quote).
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
  return `"${s.replace(/"/g, '""')}"`; // then always quote; escape embedded quotes
}

export function buildAuditCsv(items: ActivityItem[]): string {
  const header = COLS.join(",");
  const rows = items.map((it) => COLS.map((c) => csvCell(it[c])).join(","));
  return [header, ...rows].join("\r\n"); // CRLF for Excel friendliness
}
