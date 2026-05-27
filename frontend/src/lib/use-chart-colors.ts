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
}

const DARK: ChartColors = {
  amber: "#fbbf24",
  sky: "#38bdf8",
  emerald: "#34d399",
  rose: "#fb7185",
};

const LIGHT: ChartColors = {
  amber: "#92400e",
  sky: "#1d4ed8",
  emerald: "#065f46",
  rose: "#9f1239",
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
