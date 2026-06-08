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
  amber: "#f6c15a",
  sky: "#5ee7ff",
  emerald: "#24f4b6",
  rose: "#ff5f8a",
  grid: "#1f3436",
  axis: "#8ea5a5",
  tooltipBg: "#0b1719",
  tooltipBorder: "#2d4a4d",
  tooltipText: "#d7e5e2",
};

const LIGHT: ChartColors = {
  amber: "#9a5b00",
  sky: "#006b84",
  emerald: "#00785f",
  rose: "#b51f46",
  grid: "#c9d9d2",
  axis: "#49625c",
  tooltipBg: "#fbfffb",
  tooltipBorder: "#9db8ad",
  tooltipText: "#12211e",
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
