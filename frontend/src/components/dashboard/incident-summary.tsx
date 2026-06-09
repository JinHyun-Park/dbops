"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { fetchMultiClusterOverview } from "@/lib/api-client";
import { eolFor } from "@/lib/engine";
import { triage, type Level, type TriageInput } from "@/lib/cluster-triage";
import { RcaButton } from "@/components/design-system/rca-button";

interface OverviewRow extends TriageInput {
  cluster_id: string;
  engine?: string;
  engine_version?: string;
}

// Incident-first summary: when the selected cluster is not OK, lead the
// dashboard with WHY (the triage reasons) and the next action (AI RCA), so the
// DBA doesn't have to scan every panel to figure out what's wrong. Stays in
// lockstep with the Fleet page + the cluster card pill via the shared triage().
const STYLE: Record<
  Exclude<Level, "ok">,
  { wrap: string; badge: string; label: string; headline: string }
> = {
  critical: {
    wrap: "border-rose-500/50 bg-rose-500/10",
    badge: "bg-rose-500/20 text-rose-200 border-rose-500/50",
    label: "CRITICAL",
    headline: "즉시 확인이 필요한 신호가 있습니다",
  },
  warning: {
    wrap: "border-amber-500/50 bg-amber-500/10",
    badge: "bg-amber-500/20 text-amber-200 border-amber-500/50",
    label: "주의",
    headline: "주의가 필요한 신호가 있습니다",
  },
};

export function IncidentSummary({ clusterId }: { clusterId: string }) {
  const [row, setRow] = useState<OverviewRow | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchMultiClusterOverview()
        .then((r: { clusters?: OverviewRow[] }) => {
          if (cancelled) return;
          setRow(
            (r.clusters || []).find((c) => c.cluster_id === clusterId) || null,
          );
        })
        .catch(() => {});
    load();
    const iv = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId]);

  if (!row) return null;
  const t = triage(row, eolFor(row.engine, row.engine_version));
  if (t.level === "ok") return null;
  const s = STYLE[t.level];

  return (
    <div className={`mt-4 border ${s.wrap} p-4`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <span
            className={`shrink-0 px-2 py-0.5 text-[10px] font-mono tracking-wider uppercase border ${s.badge}`}
          >
            {s.label}
          </span>
          <div className="min-w-0">
            <div className="text-sm text-zinc-100 font-medium">
              {s.headline}
            </div>
            {/* The exact signals that put this cluster over the line — the same
                reasons the Fleet triage tooltip shows. */}
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {t.reasons.map((r) => (
                <span
                  key={r}
                  className="text-[11px] font-mono px-1.5 py-0.5 border border-zinc-700 bg-zinc-900/60 text-zinc-300"
                >
                  {r}
                </span>
              ))}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <RcaButton clusterId={clusterId} variant="prominent" />
          <Link
            href={`/timeline?cluster=${encodeURIComponent(clusterId)}`}
            className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-zinc-500 hover:text-zinc-200 transition-colors"
          >
            타임라인 →
          </Link>
        </div>
      </div>
    </div>
  );
}
