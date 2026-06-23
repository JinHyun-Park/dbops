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
