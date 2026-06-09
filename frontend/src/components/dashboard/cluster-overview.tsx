"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { fetchMultiClusterOverview } from "@/lib/api-client";
import { eolFor } from "@/lib/engine";
import {
  triage,
  LEVEL_RANK,
  type Level,
  type TriageInput,
} from "@/lib/cluster-triage";

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

interface OverviewRow extends TriageInput {
  cluster_id: string;
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
  const [metrics, setMetrics] = useState<Map<string, TriageInput>>(new Map());

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchMultiClusterOverview()
        .then((r: { clusters?: OverviewRow[] }) => {
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

      {/* Severity-sorted cluster chips — quick switch + peripheral awareness. */}
      <div className="flex flex-wrap gap-1.5">
        {capped.map((d) => {
          const active = d.c.cluster_id === selectedId;
          return (
            <button
              key={d.c.cluster_id}
              onClick={() => onSelect(d.c.cluster_id)}
              title={
                d.reasons.length
                  ? `${d.c.cluster_id} — ${d.reasons.join(" · ")}`
                  : d.c.cluster_id
              }
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[12px] font-mono transition-colors max-w-[260px] ${
                active
                  ? "border-emerald-500/70 bg-emerald-500/10 text-zinc-100"
                  : "border-zinc-800 bg-zinc-900/60 text-zinc-300 hover:border-zinc-600"
              }`}
            >
              <span
                className={`flex-shrink-0 w-2 h-2 rounded-full ${DOT[d.level]}`}
              />
              <span className="truncate">{d.c.cluster_id}</span>
            </button>
          );
        })}
        {overflow > 0 && (
          <Link
            href="/fleet"
            className="flex items-center px-2.5 py-1 rounded-md border border-zinc-800 bg-zinc-900/40 text-[12px] text-zinc-500 hover:text-emerald-300 hover:border-emerald-500/40 transition-colors"
          >
            +{overflow}개 더 → Fleet
          </Link>
        )}
      </div>
    </div>
  );
}
