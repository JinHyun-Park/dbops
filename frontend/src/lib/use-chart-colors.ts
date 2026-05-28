"use client";

import { useEffect, useState } from "react";

// Shared theme-aware palette for Recharts (and any other inline-styled
// SVG). Recharts injects series colors as inline `stroke` / `fill` props
// on the rendered SVG, which CSS class overrides cannot reach — so we
// have to swap the hex values themselves when the user flips to light.
//
// The light values are the darker WCAG-AA-safe counterparts of each dark
// hue (amber-800 instead of amber-400, blue-700 instead of sky-400, etc.).
// Chosen so legend text + series strokes hit > 4.5:1 against both the
// cream page bg (#f5f4ef) and the white card bg.
export interface ChartColors {
  amber: string;
  sky: string;
  emerald: string;
  rose: string;
  // Chart chrome — grid + axis tick + tooltip background/border. These
  // were hardcoded inline in several Recharts components ("#27272a",
  // "#71717a") which rendered as near-black on the light-theme cream
  // canvas, producing axis ticks the user couldn't read.
  grid: string;
  axis: string;
  tooltipBg: string;
  tooltipBorder: string;
  tooltipText: string;
}

const DARK: ChartColors = {
  amber: "#fbbf24",
  sky: "#38bdf8",
  emerald: "#34d399",
  rose: "#fb7185",
  grid: "#27272a", // zinc-800
  axis: "#71717a", // zinc-500
  tooltipBg: "#18181b", // zinc-900
  tooltipBorder: "#3f3f46", // zinc-700
  tooltipText: "#a1a1aa", // zinc-400
};

const LIGHT: ChartColors = {
  amber: "#92400e",
  sky: "#1d4ed8",
  emerald: "#065f46",
  rose: "#9f1239",
  grid: "#d6d3c7", // stone-300 — visible on cream without dominating
  axis: "#57534e", // stone-600 — readable tick labels
  tooltipBg: "#ffffff",
  tooltipBorder: "#a8a29e", // stone-400
  tooltipText: "#1c1917", // stone-900 — high contrast tooltip copy
};

function readTheme(): "dark" | "light" {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

export function useChartColors(): ChartColors {
  const [theme, setTheme] = useState<"dark" | "light">(() => readTheme());

  useEffect(() => {
    setTheme(readTheme());
    const obs = new MutationObserver(() => setTheme(readTheme()));
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => obs.disconnect();
  }, []);

  return theme === "light" ? LIGHT : DARK;
}
