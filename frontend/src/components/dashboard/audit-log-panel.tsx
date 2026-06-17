"use client";

import { useEffect, useState } from "react";
import { fetchAuditLog } from "@/lib/api-client";

interface Entry {
  id: number | string;
  action_type: string;
  tool_name: string;
  requested_by: string;
  approved_by: string;
  sql_text: string;
  status: string;
  created_at: string;
  resolved_at: string | null;
}

const ACTION_TYPES = [
  "",
  "execute_sql",
  "modify_parameter",
  "modify_scaling",
  "manage_maintenance",
];

const STATUS_STYLES: Record<string, string> = {
  approved: "text-emerald-400 bg-emerald-500/10",
  rejected: "text-rose-400 bg-rose-500/10",
  pending: "text-amber-400 bg-amber-500/10",
  executed: "text-sky-400 bg-sky-500/10",
  failed: "text-rose-400 bg-rose-500/10",
};

// Solid dot colour for the timeline rail (the badge keeps the tinted bg above).
const STATUS_DOT: Record<string, string> = {
  approved: "bg-emerald-400",
  rejected: "bg-rose-400",
  pending: "bg-amber-400",
  executed: "bg-sky-400",
  failed: "bg-rose-400",
};

// created_at is stored as a naive UTC isoformat (no tz marker); appending Z
// stops the browser from re-reading it as local time (a +9h skew in KST).
function normTs(iso: string): number {
  const norm = /Z$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + "Z";
  return new Date(norm).getTime();
}

function relTime(iso: string) {
  const m = Math.floor((Date.now() - normTs(iso)) / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  return `${Math.floor(h / 24)}일 전`;
}

function absTime(iso: string) {
  return new Date(normTs(iso)).toLocaleString();
}

function dayKey(iso: string) {
  const d = new Date(normTs(iso));
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function dayLabel(iso: string) {
  const today = new Date();
  const yest = new Date();
  yest.setDate(today.getDate() - 1);
  const k = dayKey(iso);
  if (k === dayKey(today.toISOString())) return "오늘";
  if (k === dayKey(yest.toISOString())) return "어제";
  const d = new Date(normTs(iso));
  return `${d.getMonth() + 1}월 ${d.getDate()}일`;
}

export function AuditLogPanel({ clusterId }: { clusterId: string }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [days, setDays] = useState(7);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchAuditLog(clusterId, days, filter || undefined)
      .then((d) => !cancelled && setEntries(d.audit_entries || []))
      .catch(() => !cancelled && setEntries([]))
      .finally(() => !cancelled && setLoading(false));
  }, [clusterId, days, filter]);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-center justify-between gap-4 flex-wrap">
        <div>
          <div className="text-sm text-zinc-200 font-medium">Audit Log</div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            DBA가 승인한 작업 및 변경 이력
          </div>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-xs rounded px-2 py-1"
          >
            {ACTION_TYPES.map((a) => (
              <option key={a} value={a}>
                {a || "전체 작업"}
              </option>
            ))}
          </select>
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-xs rounded px-2 py-1"
          >
            <option value={1}>1d</option>
            <option value={7}>7d</option>
            <option value={30}>30d</option>
          </select>
        </div>
      </div>
      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">불러오는 중…</div>
      ) : entries.length === 0 ? (
        <div className="p-6 text-zinc-500 text-sm">감사 기록이 없습니다</div>
      ) : (
        <div className="max-h-96 overflow-y-auto px-4 py-4">
          <ol className="relative ml-2 border-l border-zinc-800 space-y-4">
            {entries.map((e, i) => {
              const badge =
                STATUS_STYLES[e.status] || "text-zinc-400 bg-zinc-700/50";
              const dot = STATUS_DOT[e.status] || "bg-zinc-500";
              const showDay =
                i === 0 ||
                dayKey(e.created_at) !== dayKey(entries[i - 1].created_at);
              return (
                <li key={e.id} className="ml-4">
                  {showDay && (
                    <div className="-ml-4 mb-2 text-[10px] uppercase tracking-wider text-zinc-600">
                      {dayLabel(e.created_at)}
                    </div>
                  )}
                  <div className="relative">
                    <span
                      className={`absolute -left-[1.45rem] top-1.5 w-2.5 h-2.5 rounded-full ring-4 ring-zinc-950 ${dot}`}
                      title={e.status}
                    />
                    <div className="border border-zinc-800 bg-zinc-900/40 rounded p-3">
                      <div className="flex items-center justify-between gap-2 flex-wrap">
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="font-mono text-xs text-zinc-100">
                            {e.action_type}
                          </span>
                          {e.tool_name && (
                            <span className="text-[10px] text-zinc-500 font-mono truncate">
                              {e.tool_name}
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          <span
                            className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${badge}`}
                          >
                            {e.status}
                          </span>
                          <span
                            className="text-[10px] text-zinc-500 font-mono"
                            title={absTime(e.created_at)}
                          >
                            {relTime(e.created_at)}
                          </span>
                        </div>
                      </div>
                      <div className="text-[11px] text-zinc-400 mt-1">
                        요청{" "}
                        <span className="text-zinc-300">
                          {e.requested_by || "-"}
                        </span>
                        {e.approved_by && (
                          <>
                            {" → 승인 "}
                            <span className="text-zinc-300">
                              {e.approved_by}
                            </span>
                          </>
                        )}
                      </div>
                      {e.sql_text && (
                        <pre className="mt-2 text-[10px] text-zinc-300 font-mono bg-zinc-950/60 border border-zinc-800 rounded px-2 py-1 overflow-x-auto whitespace-pre-wrap break-all max-h-24">
                          {e.sql_text}
                        </pre>
                      )}
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </div>
  );
}
