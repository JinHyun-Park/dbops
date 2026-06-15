"use client";

import { useMemo } from "react";
import Link from "next/link";
import { eolFor, ENGINE_GROUP_META, ENGINE_GROUP_ORDER } from "@/lib/engine";
import {
  triage,
  LEVEL_RANK,
  type Level,
  type TriageInput,
} from "@/lib/cluster-triage";
import { useFleetOverview } from "@/lib/use-fleet-overview";
import { groupByEngineGroup, displayName } from "@/lib/group-by-family";
import { prefetchDashboard } from "@/lib/api-client";

interface ClusterInfo {
  cluster_id: string;
  engine?: string;
  engine_version?: string;
  status?: string;
  resource_name?: string;
}

interface ClusterOverviewProps {
  clusters: ClusterInfo[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

// Hard cap on chips so the dashboard header stays compact at fleet scale — the
// dashboard is a single-cluster deep dive, not the place to render 200 cards.
// The worst clusters surface first; the rest live one click away in Fleet.
const CHIP_CAP = 12;

const DOT: Record<Level, string> = {
  critical: "bg-rose-500",
  warning: "bg-amber-400",
  ok: "bg-emerald-500",
};

export function ClusterOverview({
  clusters,
  selectedId,
  onSelect,
}: ClusterOverviewProps) {
  // Shared fleet poll (deduped with the header dropdown + incident banner).
  const fleet = useFleetOverview();
  const metrics = useMemo(() => {
    const m = new Map<string, TriageInput>();
    for (const row of fleet) m.set(row.cluster_id, row);
    return m;
  }, [fleet]);

  // Decorate + severity-sort (worst first), then heat desc, then name.
  const decorated = useMemo(() => {
    return clusters
      .map((c) => {
        const input: TriageInput = metrics.get(c.cluster_id) ?? {
          status: c.status,
        };
        const t = triage(input, eolFor(c.engine, c.engine_version));
        return { c, level: t.level, heat: t.heat, reasons: t.reasons };
      })
      .sort((a, b) => {
        const r = LEVEL_RANK[b.level] - LEVEL_RANK[a.level];
        if (r !== 0) return r;
        if (b.heat !== a.heat) return b.heat - a.heat;
        return a.c.cluster_id.localeCompare(b.c.cluster_id);
      });
  }, [clusters, metrics]);

  const counts = useMemo(() => {
    let critical = 0,
      warning = 0;
    for (const d of decorated) {
      if (d.level === "critical") critical++;
      else if (d.level === "warning") warning++;
    }
    return { total: decorated.length, critical, warning };
  }, [decorated]);

  // Always keep the selected cluster visible even if it falls past the cap.
  const capped = useMemo(() => {
    const head = decorated.slice(0, CHIP_CAP);
    if (selectedId && !head.some((d) => d.c.cluster_id === selectedId)) {
      const sel = decorated.find((d) => d.c.cluster_id === selectedId);
      if (sel) head[head.length - 1] = sel;
    }
    return head;
  }, [decorated, selectedId]);

  const overflow = decorated.length - capped.length;

  if (clusters.length === 0) {
    return (
      <div className="text-sm text-zinc-500">
        등록된 클러스터가 없습니다.{" "}
        <Link href="/clusters" className="text-emerald-300 hover:underline">
          클러스터 등록 →
        </Link>
      </div>
    );
  }

  return (
    <div className="border border-zinc-800 bg-zinc-900/40 rounded-lg p-3">
      {/* Summary band — counts double as quick filters into Fleet. */}
      <div className="flex flex-wrap items-center gap-2 mb-2.5 text-[11px]">
        <span className="text-zinc-500 uppercase tracking-wider">
          {counts.total} clusters
        </span>
        {counts.critical > 0 && (
          <Link
            href="/fleet?level=critical"
            className="flex items-center gap-1.5 px-2 py-0.5 rounded-full border border-rose-500/40 bg-rose-500/10 text-rose-300 hover:border-rose-500/70 transition-colors"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-rose-500" />
            {counts.critical} critical
          </Link>
        )}
        {counts.warning > 0 && (
          <Link
            href="/fleet?level=warning"
            className="flex items-center gap-1.5 px-2 py-0.5 rounded-full border border-amber-500/40 bg-amber-500/10 text-amber-300 hover:border-amber-500/70 transition-colors"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
            {counts.warning} warning
          </Link>
        )}
        <Link
          href="/fleet"
          className="ml-auto text-zinc-500 hover:text-emerald-300 transition-colors"
        >
          Fleet 전체 →
        </Link>
      </div>

      {/* Engine-family grouped chips — each family gets a small header label
          then its severity-sorted chips. Empty families are skipped. */}
      {(() => {
        // Build a lookup from cluster_id → decorated entry for O(1) access.
        const byId = new Map(capped.map((d) => [d.c.cluster_id, d]));
        // Group the capped clusters into engine-group buckets (preserving sort
        // order within each bucket because `capped` is already severity-sorted).
        const grouped = groupByEngineGroup(capped.map((d) => d.c));
        const hasMultipleGroups =
          ENGINE_GROUP_ORDER.filter((g) => grouped[g].length > 0).length > 1;

        return (
          <div className="space-y-2">
            {ENGINE_GROUP_ORDER.map((g) => {
              const famClusters = grouped[g];
              if (famClusters.length === 0) return null;
              const meta = ENGINE_GROUP_META[g];
              return (
                <div key={g}>
                  {/* Only show the group label row when there are multiple
                      groups — keeps the UI clean for single-engine fleets. */}
                  {hasMultipleGroups && (
                    <div className="flex items-center gap-1.5 mb-1">
                      <span
                        className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${meta.accent}`}
                      />
                      <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                        {meta.label}
                      </span>
                      <span className="text-[10px] text-zinc-600 font-mono">
                        {famClusters.length}
                      </span>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1.5">
                    {famClusters.map((c) => {
                      const d = byId.get(c.cluster_id);
                      if (!d) return null;
                      const active = c.cluster_id === selectedId;
                      return (
                        <button
                          key={c.cluster_id}
                          onClick={() => onSelect(c.cluster_id)}
                          // Warm the /overview + batch-timeseries cache on hover
                          // so the click→render is already in flight (often
                          // cached) by the time the user commits to a cluster.
                          onMouseEnter={() => prefetchDashboard(c.cluster_id)}
                          title={
                            d.reasons.length
                              ? `${c.cluster_id} — ${d.reasons.join(" · ")}`
                              : c.cluster_id
                          }
                          className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[12px] font-mono transition-colors max-w-[260px] ${
                            active
                              ? "border-emerald-500/70 bg-emerald-500/10 text-zinc-100"
                              : "border-zinc-800 bg-zinc-900/60 text-zinc-300 hover:border-zinc-600"
                          }`}
                        >
                          <span
                            className={`flex-shrink-0 w-2 h-2 rounded-full ${
                              DOT[d.level]
                            }`}
                          />
                          <span className="truncate">{displayName(c)}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })}

            {overflow > 0 && (
              <Link
                href="/fleet"
                className="inline-flex items-center px-2.5 py-1 rounded-md border border-zinc-800 bg-zinc-900/40 text-[12px] text-zinc-500 hover:text-emerald-300 hover:border-emerald-500/40 transition-colors"
              >
                +{overflow}개 더 → Fleet
              </Link>
            )}
          </div>
        );
      })()}
    </div>
  );
}
