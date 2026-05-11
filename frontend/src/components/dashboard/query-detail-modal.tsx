"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { fetchQueryDetail } from "@/lib/api-client";

interface Snapshot {
  snapshot_time: string;
  calls: number | string;
  total_time_ms: number | string;
  mean_time_ms: number | string;
  rows_returned: number | string;
  shared_blks_hit: number | string;
  shared_blks_read: number | string;
  query_text: string;
}

function n(v: unknown): number {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

export function QueryDetailModal({
  clusterId,
  queryHash,
  onClose,
}: {
  clusterId: string;
  queryHash: string;
  onClose: () => void;
}) {
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetchQueryDetail(clusterId, queryHash)
      .then((d) => {
        setSnapshots(d.snapshots || []);
        setLoading(false);
      })
      .catch((e) => {
        setErr(e.message);
        setLoading(false);
      });
  }, [clusterId, queryHash]);

  const latest = snapshots[0];
  const chartData = [...snapshots]
    .reverse()
    .map((s) => ({
      ts: new Date(s.snapshot_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      mean: n(s.mean_time_ms),
      calls: n(s.calls),
    }));

  const cacheHitRatio = latest
    ? (n(latest.shared_blks_hit) / Math.max(1, n(latest.shared_blks_hit) + n(latest.shared_blks_read))) * 100
    : 0;

  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="bg-zinc-900 border border-zinc-800 max-w-4xl w-full max-h-[90vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-center px-6 py-4 border-b border-zinc-800">
          <div>
            <div className="text-xs text-zinc-500 uppercase tracking-wider">Query Detail</div>
            <div className="text-sm text-zinc-300 font-mono">{queryHash.slice(0, 16)}…</div>
          </div>
          <div className="flex items-center gap-3">
            {latest && (
              <Link
                href={`/chat?prompt=${encodeURIComponent(
                  `Run EXPLAIN (FORMAT JSON) on this query for cluster ${clusterId} and summarize the plan, highlight the most expensive node, and suggest improvements:\n\n${latest.query_text}`
                )}`}
                className="text-xs bg-sky-600 hover:bg-sky-500 text-white rounded px-3 py-1.5 transition"
              >
                Analyze in Chat
              </Link>
            )}
            <button onClick={onClose} className="text-zinc-400 hover:text-zinc-100 text-xl leading-none px-2">
              ×
            </button>
          </div>
        </div>

        <div className="overflow-y-auto p-6 space-y-6">
          {loading ? (
            <div className="text-zinc-500 text-sm">Loading...</div>
          ) : err ? (
            <div className="text-red-400 text-sm">{err}</div>
          ) : !latest ? (
            <div className="text-zinc-500 text-sm">No snapshots</div>
          ) : (
            <>
              <div>
                <div className="text-xs text-zinc-400 uppercase tracking-wider mb-2">SQL</div>
                <pre className="bg-zinc-950 border border-zinc-800 rounded p-3 text-xs text-zinc-200 font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
                  {latest.query_text}
                </pre>
              </div>

              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <Metric label="Calls" value={n(latest.calls).toLocaleString()} />
                <Metric label="Mean (ms)" value={n(latest.mean_time_ms).toFixed(2)} />
                <Metric label="Total (ms)" value={n(latest.total_time_ms).toFixed(0)} />
                <Metric label="Rows" value={n(latest.rows_returned).toLocaleString()} />
                <Metric label="Cache Hit" value={cacheHitRatio.toFixed(1) + "%"} />
                <Metric label="Blks Hit" value={n(latest.shared_blks_hit).toLocaleString()} />
                <Metric label="Blks Read" value={n(latest.shared_blks_read).toLocaleString()} />
                <Metric label="Snapshots" value={snapshots.length.toString()} />
              </div>

              {chartData.length > 1 && (
                <div>
                  <div className="text-xs text-zinc-400 uppercase tracking-wider mb-2">
                    Mean Time Trend ({snapshots.length} snapshots)
                  </div>
                  <div className="h-48 bg-zinc-950 border border-zinc-800 rounded p-2">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={chartData} margin={{ top: 8, right: 8, bottom: 4, left: -20 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#27272a" vertical={false} />
                        <XAxis dataKey="ts" stroke="#71717a" fontSize={10} />
                        <YAxis stroke="#71717a" fontSize={10} />
                        <Tooltip
                          contentStyle={{ background: "#18181b", border: "1px solid #3f3f46", fontSize: 12 }}
                          labelStyle={{ color: "#a1a1aa" }}
                        />
                        <Line type="monotone" dataKey="mean" stroke="#fbbf24" strokeWidth={2} dot={false} />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-zinc-950 border border-zinc-800 rounded p-3">
      <div className="text-xs text-zinc-500 mb-1">{label}</div>
      <div className="text-zinc-100 font-mono text-sm">{value}</div>
    </div>
  );
}
