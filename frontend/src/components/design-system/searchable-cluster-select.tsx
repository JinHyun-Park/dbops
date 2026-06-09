"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, Search } from "lucide-react";

// Controlled, searchable replacement for a native <select> of clusters in FORM
// fields (chat conversation cluster, alert/runbook pickers). Unlike the header
// ClusterDropdown this is NOT tied to the global selection — it's a plain
// value/onChange field. A native <select> with 100+ options is unscannable;
// this gives typeahead at fleet scale.
export function SearchableClusterSelect({
  value,
  onChange,
  clusters,
  placeholder = "클러스터 선택",
  className = "",
  allowAll = false,
  allLabel = "전체 클러스터",
}: {
  value: string;
  onChange: (id: string) => void;
  clusters: { cluster_id: string }[];
  placeholder?: string;
  className?: string;
  // When true, an "all clusters" entry maps to value "" — for filter fields.
  allowAll?: boolean;
  allLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node))
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

  const ql = q.trim().toLowerCase();
  const visible = ql
    ? clusters.filter((c) => c.cluster_id.toLowerCase().includes(ql))
    : clusters;

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 bg-zinc-900 text-zinc-200 border border-zinc-800 px-3 py-1.5 text-sm focus:outline-none focus:border-amber-500/60 transition-colors"
        title={value || placeholder}
      >
        <span
          className={`flex-1 min-w-0 truncate text-left font-mono ${
            value ? "text-zinc-200" : "text-zinc-500"
          }`}
        >
          {value || (allowAll ? allLabel : placeholder)}
        </span>
        <ChevronDown
          size={13}
          strokeWidth={2}
          className="flex-shrink-0 text-zinc-500"
        />
      </button>
      {open && (
        <div className="absolute z-50 mt-1 w-full min-w-[16rem] bg-zinc-900 border border-zinc-700 rounded-md shadow-2xl overflow-hidden">
          <div className="flex items-center gap-2 px-2.5 border-b border-zinc-800">
            <Search size={13} className="text-zinc-500 flex-shrink-0" />
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="검색…"
              className="w-full py-2 bg-transparent text-sm text-zinc-100 focus:outline-none placeholder:text-zinc-600"
              onKeyDown={(e) => {
                if (e.key === "Enter" && visible.length > 0) {
                  onChange(visible[0].cluster_id);
                  setOpen(false);
                  setQ("");
                }
              }}
            />
          </div>
          <div className="max-h-64 overflow-y-auto py-1">
            {allowAll && !ql && (
              <button
                type="button"
                onClick={() => {
                  onChange("");
                  setOpen(false);
                  setQ("");
                }}
                className={`w-full text-left px-3 py-1.5 text-[12px] transition-colors ${
                  value === ""
                    ? "bg-zinc-800/80 text-zinc-100"
                    : "text-zinc-300 hover:bg-zinc-800/50"
                }`}
              >
                {allLabel}
              </button>
            )}
            {visible.map((c) => (
              <button
                key={c.cluster_id}
                type="button"
                onClick={() => {
                  onChange(c.cluster_id);
                  setOpen(false);
                  setQ("");
                }}
                className={`w-full text-left px-3 py-1.5 text-[12px] font-mono truncate transition-colors ${
                  c.cluster_id === value
                    ? "bg-zinc-800/80 text-zinc-100"
                    : "text-zinc-300 hover:bg-zinc-800/50"
                }`}
              >
                {c.cluster_id}
              </button>
            ))}
            {visible.length === 0 && (
              <div className="px-3 py-5 text-center text-zinc-500 text-sm">
                {clusters.length === 0 ? "클러스터 없음" : "결과 없음"}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
