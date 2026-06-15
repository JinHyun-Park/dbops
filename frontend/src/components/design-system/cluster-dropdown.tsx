"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { ChevronDown, Database, Search } from "lucide-react";
import { eolFor, ENGINE_GROUP_META, ENGINE_GROUP_ORDER } from "@/lib/engine";
import { triage, type Level } from "@/lib/cluster-triage";
import { useSelectedCluster } from "@/lib/use-selected-cluster";
import { useFleetOverview } from "@/lib/use-fleet-overview";
import { AnchoredPopover } from "@/components/design-system/anchored-popover";
import { groupByEngineGroup, displayName } from "@/lib/group-by-family";
import { prefetchDashboard } from "@/lib/api-client";

// A real, discoverable cluster switcher: click → a popover that lists the
// clusters immediately (with a severity dot from the shared triage), with
// typeahead for large fleets. Replaces the old chip that opened the ⌘K search
// palette — ⌘K is now pages/search only, so the two no longer collide.
const DOT: Record<Level, string> = {
  critical: "bg-rose-500",
  warning: "bg-amber-400",
  ok: "bg-emerald-500",
};

function shorten(id: string, max: number): string {
  return id.length > max ? `${id.slice(0, max - 1)}…` : id;
}

export function ClusterDropdown({
  className = "",
  align = "left",
}: {
  className?: string;
  align?: "left" | "right";
}) {
  const { clusters, selected, setSelected } = useSelectedCluster();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);

  // Severity dots from the shared fleet poll (deduped with the dashboard).
  const fleet = useFleetOverview();
  const levels = useMemo(() => {
    const m = new Map<string, Level>();
    for (const row of fleet)
      m.set(
        row.cluster_id,
        triage(row, eolFor(row.engine, row.engine_version)).level,
      );
    return m;
  }, [fleet]);

  const choose = useCallback(
    (id: string) => {
      setSelected(id);
      setOpen(false);
      setQuery("");
    },
    [setSelected],
  );

  const q = query.trim().toLowerCase();
  const visible = q
    ? clusters.filter((c) => displayName(c).toLowerCase().includes(q))
    : clusters;

  // Flatten the first visible item across families for Enter-key selection.
  const firstVisible = visible[0] ?? null;

  // Group visible results by engine group for section headers.
  const grouped = useMemo(() => {
    const byGroup = groupByEngineGroup(visible);
    return ENGINE_GROUP_ORDER.map((g) => ({
      fam: g,
      meta: ENGINE_GROUP_META[g],
      items: byGroup[g],
    })).filter((g) => g.items.length > 0);
  }, [visible]);

  const selLevel = selected ? levels.get(selected) : undefined;
  // For the trigger button, find the selected cluster's displayName.
  const selectedCluster = selected
    ? clusters.find((c) => c.cluster_id === selected)
    : null;
  const selDisplay = selectedCluster ? displayName(selectedCluster) : null;

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={selected ? `${selected} — 클러스터 전환` : "클러스터 선택"}
        className="flex items-center gap-2 px-2.5 py-1.5 rounded-md border border-zinc-800 bg-zinc-900/50 hover:border-emerald-500/40 transition-colors max-w-[280px]"
      >
        {selected && selLevel ? (
          <span
            className={`flex-shrink-0 w-2 h-2 rounded-full ${DOT[selLevel]}`}
          />
        ) : (
          <Database
            size={13}
            strokeWidth={2}
            className="flex-shrink-0 text-emerald-300/70"
          />
        )}
        {selDisplay ? (
          <span className="text-[12px] font-mono text-zinc-200 truncate">
            {shorten(selDisplay, 26)}
          </span>
        ) : (
          <span className="text-[12px] text-zinc-500">클러스터 선택</span>
        )}
        <ChevronDown
          size={13}
          strokeWidth={2}
          className="flex-shrink-0 text-zinc-500"
        />
      </button>

      <AnchoredPopover
        anchorRef={rootRef}
        open={open}
        onClose={() => setOpen(false)}
        align={align}
      >
        <div className="w-72 bg-zinc-900 border border-zinc-700 rounded-lg shadow-2xl overflow-hidden">
          <div className="flex items-center gap-2 px-3 border-b border-zinc-800">
            <Search size={14} className="text-zinc-500 flex-shrink-0" />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="클러스터 검색…"
              className="w-full py-2.5 bg-transparent text-sm text-zinc-100 focus:outline-none placeholder:text-zinc-600"
              onKeyDown={(e) => {
                if (e.key === "Enter" && firstVisible)
                  choose(firstVisible.cluster_id);
              }}
            />
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {grouped.length === 0 ? (
              <div className="px-3 py-6 text-center text-zinc-500 text-sm">
                {clusters.length === 0 ? "클러스터 없음" : "결과 없음"}
              </div>
            ) : (
              grouped.map(({ fam, meta, items }) => (
                <div key={fam}>
                  {/* Family section header — a small label row that matches the
                      surrounding typography: muted caps label + dot accent. */}
                  <div className="flex items-center gap-1.5 px-3 pt-2 pb-1">
                    <span
                      className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${meta.accent}`}
                    />
                    <span className="text-[10px] uppercase tracking-wider text-zinc-500 font-medium">
                      {meta.label}
                    </span>
                    <span className="text-[10px] text-zinc-600 ml-auto">
                      {items.length}
                    </span>
                  </div>
                  {items.map((c) => {
                    const lvl = levels.get(c.cluster_id);
                    const active = c.cluster_id === selected;
                    return (
                      <button
                        key={c.cluster_id}
                        onClick={() => choose(c.cluster_id)}
                        onMouseEnter={() => prefetchDashboard(c.cluster_id)}
                        onFocus={() => prefetchDashboard(c.cluster_id)}
                        className={`w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors ${
                          active ? "bg-zinc-800/80" : "hover:bg-zinc-800/50"
                        }`}
                      >
                        <span
                          className={`flex-shrink-0 w-2 h-2 rounded-full ${
                            lvl ? DOT[lvl] : "bg-zinc-600"
                          }`}
                        />
                        <span className="flex-1 min-w-0 text-[12px] font-mono text-zinc-200 truncate">
                          {displayName(c)}
                        </span>
                        {active && (
                          <span className="text-[10px] text-emerald-300/80">
                            현재
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              ))
            )}
          </div>
        </div>
      </AnchoredPopover>
    </div>
  );
}
