"use client";

/**
 * Expandable — drop-in wrapper that adds a small "⛶ expand" button to the
 * top-right of any panel/chart. Clicking opens a modal with the same content
 * rendered at ~90vw × 90vh so users can read dense charts without leaving
 * the page.
 *
 * Usage:
 *   <Expandable title="CPU Utilization">
 *     <TimeseriesChart ... />
 *   </Expandable>
 *
 * The children render twice when expanded (once in place, once in the modal).
 * For most chart components this is fine — Recharts re-mounts cheaply. If a
 * child component is expensive or stateful, pass `freezeInline` to hide the
 * inline copy while expanded.
 */

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

interface ExpandableProps {
  title?: string;
  /** Hide the inline copy while the modal is open. Default false. */
  freezeInline?: boolean;
  /** Override the expand button's top-right offset (e.g. for charts with their own header). */
  buttonClassName?: string;
  className?: string;
  children: React.ReactNode;
}

export function Expandable({
  title,
  freezeInline,
  buttonClassName,
  className,
  children,
}: ExpandableProps) {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  // ESC to close + body scroll lock while modal open.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className={`relative ${className || ""}`}>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={title ? `Expand ${title}` : "Expand"}
        title="Expand"
        className={`absolute top-2 right-2 z-10 w-6 h-6 flex items-center justify-center rounded text-[11px] text-zinc-500 hover:text-zinc-200 hover:bg-zinc-800/70 border border-transparent hover:border-zinc-700 transition-colors print:hidden ${
          buttonClassName || ""
        }`}
      >
        ⛶
      </button>

      {/* Inline copy. Hidden while modal is open if freezeInline=true so a
          heavy chart doesn't render twice simultaneously. */}
      <div style={open && freezeInline ? { visibility: "hidden" } : undefined}>
        {children}
      </div>

      {open &&
        mounted &&
        createPortal(
          <div
            className="fixed inset-0 z-50 bg-zinc-950/85 backdrop-blur flex items-center justify-center p-4 sm:p-6 md:p-10"
            onClick={() => setOpen(false)}
            role="dialog"
            aria-modal="true"
            aria-label={title || "Expanded panel"}
          >
            <div
              // No fixed height — modal hugs the content. `max-h-[92vh]` caps
              // long content so the body scrolls inside the modal. Wrapping
              // with `overflow-hidden` keeps the rounded edge clean when the
              // body scrollbar appears.
              className="w-full max-w-7xl max-h-[92vh] bg-zinc-900 border border-zinc-700 shadow-2xl flex flex-col overflow-hidden"
              onClick={(e) => e.stopPropagation()}
            >
              <header className="flex items-center justify-between px-4 py-2.5 border-b border-zinc-800 flex-shrink-0">
                <div className="text-xs text-zinc-300 font-mono uppercase tracking-wider">
                  {title || "expanded view"}
                </div>
                <button
                  onClick={() => setOpen(false)}
                  className="text-zinc-400 hover:text-zinc-100 text-xl leading-none px-2"
                  aria-label="Close"
                >
                  ×
                </button>
              </header>
              <div className="overflow-auto p-4 min-h-0">{children}</div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
