"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Database, Search } from "lucide-react";
import { eolFor } from "@/lib/engine";
import { triage, type Level } from "@/lib/cluster-triage";
import { useSelectedCluster } from "@/lib/use-selected-cluster";
import { useFleetOverview } from "@/lib/use-fleet-overview";

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

  // Close on outside click + Escape.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node))
        setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

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
    ? clusters.filter((c) => c.cluster_id.toLowerCase().includes(q))
    : clusters;
  const selLevel = selected ? levels.get(selected) : undefined;

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
        {selected ? (
          <span className="text-[12px] font-mono text-zinc-200 truncate">
            {shorten(selected, 26)}
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

      {open && (
        <div
          className={`absolute z-50 mt-1.5 w-72 bg-zinc-900 border border-zinc-700 rounded-lg shadow-2xl overflow-hidden ${
            align === "right" ? "right-0" : "left-0"
          }`}
        >
          <div className="flex items-center gap-2 px-3 border-b border-zinc-800">
            <Search size={14} className="text-zinc-500 flex-shrink-0" />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="클러스터 검색…"
              className="w-full py-2.5 bg-transparent text-sm text-zinc-100 focus:outline-none placeholder:text-zinc-600"
              onKeyDown={(e) => {
                if (e.key === "Enter" && visible.length > 0)
                  choose(visible[0].cluster_id);
              }}
            />
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {visible.map((c) => {
              const lvl = levels.get(c.cluster_id);
              const active = c.cluster_id === selected;
              return (
                <button
                  key={c.cluster_id}
                  onClick={() => choose(c.cluster_id)}
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
                    {c.cluster_id}
                  </span>
                  {active && (
                    <span className="text-[10px] text-emerald-300/80">
                      현재
                    </span>
                  )}
                </button>
              );
            })}
            {visible.length === 0 && (
              <div className="px-3 py-6 text-center text-zinc-500 text-sm">
                {clusters.length === 0 ? "클러스터 없음" : "결과 없음"}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
