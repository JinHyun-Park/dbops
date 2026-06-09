"use client";

import { Database } from "lucide-react";

// Drop-in replacement for the per-page flat <select> of clusters. Shows the
// current (global) cluster and opens the ⌘K palette to switch — a typeahead
// that scales to dozens/hundreds, vs a dropdown you scroll. The actual
// selection lives in the shared store (see useSelectedCluster); this is just
// the trigger + current-value display.
function openPalette() {
  window.dispatchEvent(new CustomEvent("dbops:open-command-palette"));
}

export function ClusterPicker({
  selected,
  className = "",
}: {
  selected: string | null;
  className?: string;
}) {
  const short =
    selected && selected.length > 32 ? `${selected.slice(0, 30)}…` : selected;
  return (
    <button
      type="button"
      onClick={openPalette}
      title={
        selected ? `${selected} — 클릭 또는 ⌘K로 전환` : "클러스터 선택 (⌘K)"
      }
      className={`flex items-center gap-2 px-2.5 py-1.5 border border-zinc-700 bg-zinc-950 hover:border-emerald-500/40 text-xs transition-colors ${className}`}
    >
      <Database
        size={13}
        strokeWidth={2}
        className="text-emerald-300/70 flex-shrink-0"
      />
      {selected ? (
        <span className="font-mono text-zinc-200 truncate max-w-[220px]">
          {short}
        </span>
      ) : (
        <span className="text-zinc-500">클러스터 선택</span>
      )}
      <kbd className="hidden sm:inline ml-0.5 text-[10px] font-sans text-zinc-600 border border-zinc-700 rounded px-1 py-px bg-zinc-900/70">
        ⌘K
      </kbd>
    </button>
  );
}
