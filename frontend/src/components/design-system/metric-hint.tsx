"use client";

import { useState } from "react";
import { metricDef } from "@/lib/metric-glossary";

// MetricHint — a small "?" affordance that reveals a metric's
// definition on hover/focus. Pure CSS-positioned popover (no portal,
// no library) so it drops into any flex row. Renders nothing if the
// metric isn't in the glossary, so callers can wire it unconditionally.
//
// Accessibility: the trigger is a <button> (keyboard-focusable) and the
// popover is also shown on focus, not just hover, so it works without
// a mouse. aria-label carries the metric name for screen readers.
export function MetricHint({ metric }: { metric: string }) {
  const def = metricDef(metric);
  const [open, setOpen] = useState(false);
  if (!def) return null;

  return (
    <span className="relative inline-flex">
      <button
        type="button"
        aria-label={`${def.label} 설명`}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        className="w-3.5 h-3.5 inline-flex items-center justify-center rounded-full border border-zinc-700 text-[9px] leading-none text-zinc-500 hover:border-zinc-500 hover:text-zinc-300 transition-colors"
      >
        ?
      </button>
      {open && (
        <span
          role="tooltip"
          className="absolute left-1/2 -translate-x-1/2 bottom-[calc(100%+6px)] z-50 w-64 p-3 bg-zinc-950 border border-zinc-700 shadow-xl text-left pointer-events-none"
        >
          <span className="block text-[11px] font-medium text-zinc-100 mb-1">
            {def.label}
            {def.unit && (
              <span className="ml-1.5 font-mono text-[10px] text-zinc-500">
                {def.unit}
              </span>
            )}
          </span>
          <span className="block text-[11px] text-zinc-300 leading-relaxed mb-1.5">
            {def.what}
          </span>
          <span className="block text-[11px] text-zinc-500 leading-relaxed">
            {def.why}
          </span>
        </span>
      )}
    </span>
  );
}
