"use client";

import { useState, useEffect } from "react";
import { ClusterOverview } from "@/components/dashboard/cluster-overview";
import { MetricCard } from "@/components/design-system/metric-card";
import { fetchClusters, fetchDashboard } from "@/lib/api-client";

interface DashboardData {
  cluster?: { engine_version?: string; status?: string; storage_size_gb?: number | string };
  metrics?: { metric_type: string; avg_val: number | string; max_val: number | string }[];
  top_queries?: { query_hash: string; query_text: string; calls: number | string; total_time_ms: number | string; mean_time_ms: number | string }[];
}

function safeNum(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

export default function DashboardPage() {
  const [clusters, setClusters] = useState<{ cluster_id: string; engine?: string; status?: string }[]>([]);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [dashboardData, setDashboardData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchClusters()
      .then(setClusters)
      .catch((e) => setError(`Failed to load clusters: ${e.message}`));
  }, []);

  useEffect(() => {
    if (!selectedCluster) return;
    setDashboardData(null);
    setError(null);
    let cancelled = false;

    const load = () => {
      fetchDashboard(selectedCluster)
        .then((d) => {
          if (!cancelled) setDashboardData(d);
        })
        .catch((e) => {
          if (!cancelled) setError(`Failed to load dashboard: ${e.message}`);
        });
    };
    load();
    const interval = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [selectedCluster]);

  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 p-6">
      <h1 className="text-2xl font-bold mb-6">Dashboard</h1>

      <ClusterOverview
        clusters={clusters}
        selectedId={selectedCluster}
        onSelect={setSelectedCluster}
      />

      {error && (
        <div className="mt-6 bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300 text-sm">
          {error}
        </div>
      )}

      {selectedCluster && !dashboardData && !error && (
        <div className="mt-6 text-zinc-500">Loading metrics for {selectedCluster}...</div>
      )}

      {dashboardData && (
        <div className="mt-6 space-y-6">
          <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-4">
            <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">Cluster Info</div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div><span className="text-zinc-500">Engine Version:</span> <span className="text-zinc-200">{dashboardData.cluster?.engine_version || "-"}</span></div>
              <div><span className="text-zinc-500">Status:</span> <span className="text-emerald-400">{dashboardData.cluster?.status || "-"}</span></div>
              <div><span className="text-zinc-500">Storage:</span> <span className="text-zinc-200">{dashboardData.cluster?.storage_size_gb ?? "-"} GB</span></div>
            </div>
          </div>

          {dashboardData.metrics && dashboardData.metrics.length > 0 ? (
            <div>
              <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">Recent Metrics (last 1h)</div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                {dashboardData.metrics.map((m) => (
                  <MetricCard
                    key={m.metric_type}
                    label={`${m.metric_type} (avg)`}
                    value={safeNum(m.avg_val).toFixed(3)}
                  />
                ))}
              </div>
            </div>
          ) : (
            <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-6 text-center text-zinc-500">
              No metrics collected yet (ETL runs every 5 minutes)
            </div>
          )}

          {dashboardData.top_queries && dashboardData.top_queries.length > 0 && (
            <div>
              <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">Top Queries by Total Time</div>
              <div className="bg-zinc-800 border border-zinc-700 rounded-lg overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-850 border-b border-zinc-700">
                    <tr>
                      <th className="text-left px-4 py-2 text-zinc-400 font-medium">Query</th>
                      <th className="text-right px-4 py-2 text-zinc-400 font-medium">Calls</th>
                      <th className="text-right px-4 py-2 text-zinc-400 font-medium">Total Time (ms)</th>
                      <th className="text-right px-4 py-2 text-zinc-400 font-medium">Mean (ms)</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-700">
                    {dashboardData.top_queries.slice(0, 20).map((q, i) => (
                      <tr key={`${q.query_hash}-${i}`} className="hover:bg-zinc-750">
                        <td className="px-4 py-2 text-zinc-200 font-mono text-xs truncate max-w-md" title={q.query_text || ""}>
                          {q.query_text || "(unknown)"}
                        </td>
                        <td className="px-4 py-2 text-right text-zinc-300">{safeNum(q.calls).toLocaleString()}</td>
                        <td className="px-4 py-2 text-right text-zinc-300">{safeNum(q.total_time_ms).toFixed(0)}</td>
                        <td className="px-4 py-2 text-right text-zinc-300">{safeNum(q.mean_time_ms).toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
