"use client";

import { StatusBadge } from "@/components/design-system/status-badge";

interface ClusterInfo {
  cluster_id: string;
  engine?: string;
  engine_version?: string;
  status?: string;
}

interface ClusterOverviewProps {
  clusters: ClusterInfo[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

function mapStatus(
  status: string,
): "healthy" | "warning" | "critical" | "unknown" {
  if (status === "available") return "healthy";
  if (status === "backing-up" || status === "modifying") return "warning";
  if (status === "stopped" || status === "failed") return "critical";
  return "unknown";
}

export function ClusterOverview({
  clusters,
  selectedId,
  onSelect,
}: ClusterOverviewProps) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {clusters.map((c) => (
        <button
          key={c.cluster_id}
          onClick={() => onSelect(c.cluster_id)}
          className={`text-left bg-zinc-800 border p-4 transition-colors hover:border-blue-500 ${
            selectedId === c.cluster_id ? "border-blue-500" : "border-zinc-800"
          }`}
        >
          <div className="flex items-center justify-between mb-2">
            <span className="font-medium text-zinc-100">{c.cluster_id}</span>
            <StatusBadge status={mapStatus(c.status || "")} />
          </div>
          <div className="text-sm text-zinc-400">
            {c.engine} {c.engine_version || ""}
          </div>
        </button>
      ))}
    </div>
  );
}
