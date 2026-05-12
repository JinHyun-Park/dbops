"use client";

import { useEffect, useState } from "react";

type Theme = "dark" | "light";
const STORAGE_KEY = "dbops_theme";

function applyTheme(t: Theme) {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", t);
}

// Sun / moon icons — lightweight inline SVG so we don't pull lucide for
// just two glyphs. Stroke width 1.75 reads correctly at 14px.
function SunIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}

function MoonIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const [theme, setTheme] = useState<Theme>("dark");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const stored =
      (typeof window !== "undefined" && (localStorage.getItem(STORAGE_KEY) as Theme | null)) ||
      "dark";
    setTheme(stored);
    applyTheme(stored);
    setMounted(true);
  }, []);

  const flip = (next: Theme) => {
    if (next === theme) return;
    setTheme(next);
    applyTheme(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* quota — ignore */
    }
  };

  if (compact) {
    // Mobile / tight spaces — single icon button, swaps on click.
    return (
      <button
        onClick={() => flip(theme === "dark" ? "light" : "dark")}
        aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        title={theme === "dark" ? "Light mode" : "Dark mode"}
        className="w-8 h-8 flex items-center justify-center border border-zinc-800 hover:border-zinc-600 text-zinc-300 hover:text-zinc-100 transition-colors"
      >
        {mounted ? (
          theme === "dark" ? (
            <SunIcon className="w-4 h-4" />
          ) : (
            <MoonIcon className="w-4 h-4" />
          )
        ) : (
          <span className="w-4 h-4" />
        )}
      </button>
    );
  }

  // Desktop — segmented two-state pill: both icons visible, current is filled.
  const isDark = theme === "dark";
  return (
    <div
      role="tablist"
      aria-label="Theme"
      className="inline-flex items-center gap-0.5 border border-zinc-800 p-0.5"
    >
      <button
        role="tab"
        aria-selected={isDark}
        onClick={() => flip("dark")}
        title="Dark mode"
        className={`w-7 h-7 flex items-center justify-center transition-colors ${
          isDark
            ? "bg-zinc-800 text-zinc-100"
            : "text-zinc-500 hover:text-zinc-200"
        }`}
      >
        <MoonIcon className="w-3.5 h-3.5" />
      </button>
      <button
        role="tab"
        aria-selected={!isDark}
        onClick={() => flip("light")}
        title="Light mode"
        className={`w-7 h-7 flex items-center justify-center transition-colors ${
          !isDark
            ? "bg-zinc-800 text-zinc-100"
            : "text-zinc-500 hover:text-zinc-200"
        }`}
      >
        <SunIcon className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}
