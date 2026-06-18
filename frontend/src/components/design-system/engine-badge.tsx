"use client";

// Shared engine badge pill — reused by ClusterDropdown and FleetRow.
// Renders a compact colored pill with the engine short label + optional version.
// Colors come from engineBadge() in @/lib/engine to stay in sync with the Fleet
// table; don't duplicate the palette here.

import { engineBadge } from "@/lib/engine";

interface EngineBadgeProps {
  engine?: string | null;
  version?: string | null;
  /** "full" renders label+version (Fleet table style); "compact" renders short label only */
  size?: "full" | "compact";
  className?: string;
}

export function EngineBadge({
  engine,
  version,
  size = "compact",
  className = "",
}: EngineBadgeProps) {
  const badge = engineBadge(engine);
  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 border text-[10px] font-mono uppercase tracking-wider ${badge.classes} ${className}`}
      title={engine ? `${engine}${version ? " " + version : ""}` : undefined}
    >
      <span className={`w-1 h-1 rounded-full flex-shrink-0 ${badge.accent}`} />
      {size === "full" ? (
        <>
          {badge.label}
          {version && (
            <span className="text-zinc-300/80 normal-case font-normal ml-0.5">
              {version}
            </span>
          )}
        </>
      ) : (
        badge.short
      )}
    </span>
  );
}
