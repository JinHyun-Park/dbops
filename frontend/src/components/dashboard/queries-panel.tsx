"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchSlowQueries } from "@/lib/api-client";
import { QueryDetailModal } from "./query-detail-modal";

interface Query {
  query_hash: string;
  query_text: string;
  calls: number | string;
  total_time_ms: number | string;
  mean_time_ms: number | string;
  rows_returned?: number | string;
}

function n(v: unknown): number {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

type SortKey = "total_time_ms" | "mean_time_ms" | "calls";

export function QueriesPanel({
  clusterId,
  topQueries,
}: {
  clusterId: string;
  topQueries: Query[];
}) {
  const [tab, setTab] = useState<"top" | "slow">("top");
  const [sort, setSort] = useState<SortKey>("total_time_ms");
  const [slow, setSlow] = useState<Query[]>([]);
  const [threshold, setThreshold] = useState(100);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (tab !== "slow") return;
    fetchSlowQueries(clusterId, 1, threshold)
      .then((d) => setSlow(d.slow_queries || []))
      .catch(() => setSlow([]));
  }, [clusterId, tab, threshold]);

  const rows = useMemo(() => {
    const src = tab === "top" ? topQueries : slow;
    const filtered = filter
      ? src.filter((q) => (q.query_text || "").toLowerCase().includes(filter.toLowerCase()))
      : src;
    return [...filtered].sort((a, b) => n(b[sort]) - n(a[sort]));
  }, [tab, topQueries, slow, sort, filter]);

  return (
    <>
      <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-800 gap-4 flex-wrap">
          <div className="flex items-center gap-1">
            <TabBtn active={tab === "top"} onClick={() => setTab("top")}>
              Top Queries
            </TabBtn>
            <TabBtn active={tab === "slow"} onClick={() => setTab("slow")}>
              Slow Queries
            </TabBtn>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {tab === "slow" && (
              <select
                value={threshold}
                onChange={(e) => setThreshold(Number(e.target.value))}
                className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-xs rounded px-2 py-1"
              >
                <option value={50}>≥ 50ms</option>
                <option value={100}>≥ 100ms</option>
                <option value={500}>≥ 500ms</option>
                <option value={1000}>≥ 1s</option>
                <option value={5000}>≥ 5s</option>
              </select>
            )}
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as SortKey)}
              className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-xs rounded px-2 py-1"
            >
              <option value="total_time_ms">Sort: Total Time</option>
              <option value="mean_time_ms">Sort: Mean Time</option>
              <option value="calls">Sort: Calls</option>
            </select>
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="filter SQL..."
              className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-xs rounded px-2 py-1 w-48"
            />
          </div>
        </div>
        {rows.length === 0 ? (
          <div className="p-6 text-center text-zinc-500 text-sm">no queries</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/50 border-b border-zinc-800">
              <tr>
                <th className="text-left px-4 py-2 text-zinc-400 font-medium">Query</th>
                <th className="text-right px-4 py-2 text-zinc-400 font-medium w-24">Calls</th>
                <th className="text-right px-4 py-2 text-zinc-400 font-medium w-28">Total (ms)</th>
                <th className="text-right px-4 py-2 text-zinc-400 font-medium w-24">Mean (ms)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-700">
              {rows.map((q, i) => (
                <tr
                  key={`${q.query_hash}-${i}`}
                  className="hover:bg-zinc-900/40 cursor-pointer transition"
                  onClick={() => setSelected(q.query_hash)}
                >
                  <td
                    className="px-4 py-2 text-zinc-200 font-mono text-xs truncate max-w-md"
                    title={q.query_text || ""}
                  >
                    {q.query_text || "(unknown)"}
                  </td>
                  <td className="px-4 py-2 text-right text-zinc-300 font-mono">
                    {n(q.calls).toLocaleString()}
                  </td>
                  <td className="px-4 py-2 text-right text-zinc-300 font-mono">
                    {n(q.total_time_ms).toFixed(0)}
                  </td>
                  <td
                    className={`px-4 py-2 text-right font-mono ${
                      n(q.mean_time_ms) > 1000
                        ? "text-rose-400"
                        : n(q.mean_time_ms) > 100
                        ? "text-amber-400"
                        : "text-zinc-300"
                    }`}
                  >
                    {n(q.mean_time_ms).toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {selected && (
        <QueryDetailModal
          clusterId={clusterId}
          queryHash={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </>
  );
}

function TabBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1 rounded text-xs transition ${
        active
          ? "bg-zinc-100 text-zinc-900"
          : "text-zinc-400 hover:text-zinc-200"
      }`}
    >
      {children}
    </button>
  );
}
