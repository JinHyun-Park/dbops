"use client";

import { useState, useEffect, useCallback } from "react";
import { StatusBadge } from "@/components/design-system/status-badge";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:3001";

function mapStatus(status: string): "healthy" | "warning" | "critical" | "unknown" {
  if (status === "available") return "healthy";
  if (status === "backing-up" || status === "modifying") return "warning";
  if (status === "stopped" || status === "failed") return "critical";
  return "unknown";
}

export default function ClustersPage() {
  const [clusters, setClusters] = useState<any[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ cluster_id: "", account_id: "", region: "ap-northeast-2", engine: "aurora-postgresql", spoke_role_arn: "" });

  const loadClusters = useCallback(() => {
    fetch(`${API_BASE}/api/clusters`).then((r) => r.json()).then(setClusters).catch(console.error);
  }, []);

  useEffect(() => { loadClusters(); }, [loadClusters]);

  const handleRegister = async () => {
    await fetch(`${API_BASE}/api/clusters`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    setShowForm(false);
    setForm({ cluster_id: "", account_id: "", region: "ap-northeast-2", engine: "aurora-postgresql", spoke_role_arn: "" });
    loadClusters();
  };

  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 p-6">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">Clusters</h1>
        <button
          onClick={() => setShowForm(!showForm)}
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-500 transition-colors"
        >
          + 클러스터 등록
        </button>
      </div>

      {showForm && (
        <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-4 mb-6">
          <div className="grid grid-cols-2 gap-4">
            <input placeholder="Cluster ID" value={form.cluster_id} onChange={(e) => setForm({ ...form, cluster_id: e.target.value })} className="bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-sm" />
            <input placeholder="Account ID" value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.target.value })} className="bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-sm" />
            <input placeholder="Region" value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })} className="bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-sm" />
            <select value={form.engine} onChange={(e) => setForm({ ...form, engine: e.target.value })} className="bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-sm">
              <option value="aurora-postgresql">Aurora PostgreSQL</option>
              <option value="aurora-mysql">Aurora MySQL</option>
            </select>
            <input placeholder="Spoke Role ARN (optional)" value={form.spoke_role_arn} onChange={(e) => setForm({ ...form, spoke_role_arn: e.target.value })} className="col-span-2 bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-sm" />
          </div>
          <div className="flex gap-2 mt-4">
            <button onClick={handleRegister} className="px-4 py-2 bg-emerald-600 text-white text-sm rounded hover:bg-emerald-500">등록</button>
            <button onClick={() => setShowForm(false)} className="px-4 py-2 bg-zinc-700 text-zinc-300 text-sm rounded hover:bg-zinc-600">취소</button>
          </div>
        </div>
      )}

      <div className="bg-zinc-800 border border-zinc-700 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-zinc-750">
            <tr className="border-b border-zinc-700">
              <th className="text-left px-4 py-3 text-zinc-400 font-medium">Cluster</th>
              <th className="text-left px-4 py-3 text-zinc-400 font-medium">Engine</th>
              <th className="text-left px-4 py-3 text-zinc-400 font-medium">Account</th>
              <th className="text-left px-4 py-3 text-zinc-400 font-medium">Region</th>
              <th className="text-left px-4 py-3 text-zinc-400 font-medium">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-700">
            {clusters.map((c) => (
              <tr key={c.cluster_id} className="hover:bg-zinc-750 transition-colors">
                <td className="px-4 py-3 text-zinc-100 font-mono">{c.cluster_id}</td>
                <td className="px-4 py-3 text-zinc-300">{c.engine || "-"}</td>
                <td className="px-4 py-3 text-zinc-300">{c.account_id}</td>
                <td className="px-4 py-3 text-zinc-300">{c.region}</td>
                <td className="px-4 py-3"><StatusBadge status={mapStatus(c.status || "")} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
