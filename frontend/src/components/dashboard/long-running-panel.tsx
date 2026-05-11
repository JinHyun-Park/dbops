"use client";

import { useEffect, useState } from "react";
import { fetchLongRunningQueries } from "@/lib/api-client";

interface Query {
  pid: number | string;
  username: string;
  state: string;
  duration_sec: number | string;
  xact_duration_sec: number | string;
  query_text: string;
  wait_event_type: string;
  wait_event: string;
  client_addr: string;
  snapshot_time: string;
}

function n(v: unknown) {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

function fmtDuration(sec: number) {
  if (sec < 60) return `${sec.toFixed(0)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.floor(sec % 60)}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

export function LongRunningPanel({ clusterId }: { clusterId: string }) {
  const [items, setItems] = useState<Query[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchLongRunningQueries(clusterId)
        .then((d) => !cancelled && setItems(d.queries || []))
        .catch(() => !cancelled && setItems([]))
        .finally(() => !cancelled && setLoading(false));
    load();
    const iv = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId]);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800">
        <div className="text-xs text-zinc-400 uppercase tracking-wider">Long Running Queries</div>
        <div className="text-[11px] text-zinc-500 mt-0.5">
          active queries running &gt; 5 seconds (last 15 minutes)
        </div>
      </div>
      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">Loading...</div>
      ) : items.length === 0 ? (
        <div className="p-6 text-zinc-500 text-sm">no long-running queries detected</div>
      ) : (
        <div className="max-h-96 overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/50 border-b border-zinc-800 sticky top-0">
              <tr>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium w-16">PID</th>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium w-24">User</th>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium w-24">State</th>
                <th className="text-right px-3 py-2 text-zinc-400 font-medium w-24">Duration</th>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium w-32">Wait</th>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium">Query</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-700">
              {items.map((q, i) => {
                const dur = n(q.duration_sec);
                const durColor =
                  dur > 300 ? "text-rose-400" : dur > 60 ? "text-amber-400" : "text-zinc-300";
                const stateColor =
                  q.state === "active"
                    ? "text-emerald-400"
                    : q.state?.includes("idle in transaction")
                    ? "text-rose-400"
                    : "text-zinc-400";
                return (
                  <tr key={`${q.pid}-${q.snapshot_time}-${i}`} className="hover:bg-zinc-900/40">
                    <td className="px-3 py-2 text-zinc-300 font-mono text-xs">{n(q.pid)}</td>
                    <td className="px-3 py-2 text-zinc-300 font-mono text-xs">{q.username}</td>
                    <td className={`px-3 py-2 font-mono text-xs ${stateColor}`}>{q.state}</td>
                    <td className={`px-3 py-2 text-right font-mono text-xs ${durColor}`}>
                      {fmtDuration(dur)}
                    </td>
                    <td className="px-3 py-2 text-zinc-400 font-mono text-[11px]">
                      {q.wait_event_type ? (
                        <>
                          <span className="text-zinc-500">{q.wait_event_type}:</span>
                          {q.wait_event}
                        </>
                      ) : (
                        "-"
                      )}
                    </td>
                    <td
                      className="px-3 py-2 text-zinc-200 font-mono text-xs truncate max-w-md"
                      title={q.query_text || ""}
                    >
                      {q.query_text}
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
