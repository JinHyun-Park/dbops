"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

// Body-level portal popover anchored to a trigger element.
//
// Why a portal instead of `absolute` inside the trigger's tree: an ancestor
// with backdrop-filter / transform creates a stacking context, and a z-50
// popover trapped inside it can PAINT fine yet be HIT-TESTED below later
// siblings — the header cluster dropdown looked open but every real click on
// its options fell through to the page underneath (only synthetic .click()
// worked, which is why earlier testing missed it). Rendering at document.body
// with an explicit z-index is immune to any ancestor stacking context.
export function AnchoredPopover({
  anchorRef,
  open,
  onClose,
  align = "left",
  matchWidth = false,
  className = "",
  children,
}: {
  // The trigger's wrapper — used for positioning AND excluded from
  // outside-click closing (the trigger toggles itself).
  anchorRef: React.RefObject<HTMLElement | null>;
  open: boolean;
  onClose: () => void;
  align?: "left" | "right";
  // Size the popover to the trigger width (min 16rem) — for form fields.
  matchWidth?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  const popRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{
    top: number;
    left?: number;
    right?: number;
    width?: number;
  } | null>(null);

  const measure = useCallback(() => {
    const a = anchorRef.current;
    if (!a) return;
    const r = a.getBoundingClientRect();
    const base =
      align === "right"
        ? { top: r.bottom + 6, right: Math.max(8, window.innerWidth - r.right) }
        : { top: r.bottom + 6, left: r.left };
    setPos(matchWidth ? { ...base, width: Math.max(r.width, 256) } : base);
  }, [anchorRef, align, matchWidth]);

  // Position on open and track scroll/resize (capture catches inner scrollers
  // like the app's <main>) so the menu follows its trigger.
  useLayoutEffect(() => {
    if (!open) return;
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [open, measure]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (anchorRef.current?.contains(t)) return;
      if (popRef.current?.contains(t)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onClose, anchorRef]);

  if (!open || !pos || typeof document === "undefined") return null;
  return createPortal(
    <div
      ref={popRef}
      style={{
        position: "fixed",
        top: pos.top,
        left: pos.left,
        right: pos.right,
        width: pos.width,
        zIndex: 60,
      }}
      className={className}
    >
      {children}
    </div>,
    document.body,
  );
}
