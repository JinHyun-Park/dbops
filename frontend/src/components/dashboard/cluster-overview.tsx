"use client";

import { useEffect, useState } from "react";
import { StatusBadge } from "@/components/design-system/status-badge";
import { fetchMultiClusterOverview } from "@/lib/api-client";
import { eolFor } from "@/lib/engine";
import { triage, type Level, type TriageInput } from "@/lib/cluster-triage";

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

// The card pill reflects OPERATIONAL severity (same triage as the Fleet page),
// not just the RDS lifecycle status — a cluster can be RDS-"available" yet be
// pegged at 95% CPU with deadlocks, which must not read as "healthy".
function levelToBadge(level: Level): "healthy" | "warning" | "critical" {
  return level === "critical"
    ? "critical"
    : level === "warning"
      ? "warning"
      : "healthy";
}

export function ClusterOverview({
  clusters,
  selectedId,
  onSelect,
}: ClusterOverviewProps) {
  // cluster_id -> live triage signals from the multi-cluster overview endpoint
  // (the SAME source the Fleet page reads, so the two surfaces never disagree).
  const [metrics, setMetrics] = useState<Map<string, TriageInput>>(new Map());

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchMultiClusterOverview()
        .then((r: { clusters?: (TriageInput & { cluster_id: string })[] }) => {
          if (cancelled) return;
          const m = new Map<string, TriageInput>();
          for (const row of r.clusters || []) m.set(row.cluster_id, row);
          setMetrics(m);
        })
        .catch(() => {});
    load();
    const iv = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, []);

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {clusters.map((c) => {
        const eol = eolFor(c.engine, c.engine_version);
        // Prefer live metrics; fall back to RDS lifecycle status alone until the
        // overview lands so a card never falsely flips to "healthy" early.
        const input: TriageInput = metrics.get(c.cluster_id) ?? {
          status: c.status,
        };
        const t = triage(input, eol);
        const lifecycle = c.status || "";
        const tip =
          (t.reasons.length ? t.reasons.join(" · ") : "all signals nominal") +
          (lifecycle ? ` — RDS: ${lifecycle}` : "");
        return (
          <button
            key={c.cluster_id}
            onClick={() => onSelect(c.cluster_id)}
            title={tip}
            className={`text-left bg-zinc-800 border p-5 rounded-lg transition-all hover:border-emerald-500/60 hover:-translate-y-0.5 ${
              selectedId === c.cluster_id
                ? "border-emerald-500/70 shadow-[0_0_0_1px_rgba(36,244,182,0.18),0_16px_40px_rgba(0,0,0,0.22)]"
                : "border-zinc-800"
            }`}
          >
            <div className="flex items-center justify-between mb-2">
              <span className="font-medium text-zinc-100">{c.cluster_id}</span>
              <StatusBadge status={levelToBadge(t.level)} />
            </div>
            <div className="text-sm text-zinc-400">
              {c.engine} {c.engine_version || ""}
            </div>
            {/* RDS lifecycle status stays visible when it's anything other than
                the steady "available" so an in-flight modify/backup isn't hidden
                behind the operational pill. */}
            {lifecycle && lifecycle !== "available" && (
              <div className="text-[11px] text-amber-300/80 mt-1 font-mono">
                RDS: {lifecycle}
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}
