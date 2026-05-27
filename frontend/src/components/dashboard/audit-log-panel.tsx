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

function relTime(iso: string) {
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  return `${Math.floor(h / 24)}일 전`;
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
          <div className="text-xs text-zinc-400 uppercase tracking-wider">
            Audit Log
          </div>
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
        <div className="max-h-96 overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/50 border-b border-zinc-800 sticky top-0">
              <tr>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                  시각
                </th>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                  작업
                </th>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                  요청자
                </th>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                  승인자
                </th>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                  상태
                </th>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                  SQL
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-700">
              {entries.map((e) => {
                const sty =
                  STATUS_STYLES[e.status] || "text-zinc-400 bg-zinc-700/50";
                return (
                  <tr key={e.id} className="hover:bg-zinc-900/40">
                    <td className="px-3 py-2 text-zinc-400 font-mono text-xs whitespace-nowrap">
                      {relTime(e.created_at)}
                    </td>
                    <td className="px-3 py-2 text-zinc-200 font-mono text-xs">
                      {e.action_type}
                      {e.tool_name && (
                        <div className="text-[10px] text-zinc-500">
                          {e.tool_name}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2 text-zinc-300 text-xs">
                      {e.requested_by || "-"}
                    </td>
                    <td className="px-3 py-2 text-zinc-300 text-xs">
                      {e.approved_by || "-"}
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${sty}`}
                      >
                        {e.status}
                      </span>
                    </td>
                    <td
                      className="px-3 py-2 text-zinc-200 font-mono text-xs truncate max-w-md"
                      title={e.sql_text || ""}
                    >
                      {e.sql_text || "-"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
