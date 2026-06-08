"use client";

import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";

type Theme = "dark" | "light";
const STORAGE_KEY = "dbops_theme";

function applyTheme(t: Theme) {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", t);
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const [theme, setTheme] = useState<Theme>("dark");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const stored =
      (typeof window !== "undefined" &&
        (localStorage.getItem(STORAGE_KEY) as Theme | null)) ||
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
        aria-label={
          theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
        }
        title={theme === "dark" ? "Light mode" : "Dark mode"}
        className="w-8 h-8 flex items-center justify-center rounded-md border border-zinc-800 bg-zinc-900/40 hover:border-zinc-600 text-zinc-300 hover:text-zinc-100 transition-colors"
      >
        {mounted ? (
          theme === "dark" ? (
            <Sun className="w-4 h-4" />
          ) : (
            <Moon className="w-4 h-4" />
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
      className="inline-flex items-center gap-0.5 border border-zinc-800 bg-zinc-900/50 p-0.5 rounded-md"
    >
      <button
        role="tab"
        aria-selected={isDark}
        onClick={() => flip("dark")}
        title="Dark mode"
        className={`w-7 h-7 flex items-center justify-center transition-colors ${
          isDark
            ? "bg-zinc-800 text-zinc-100 shadow-sm"
            : "text-zinc-500 hover:text-zinc-200"
        }`}
      >
        <Moon className="w-3.5 h-3.5" />
      </button>
      <button
        role="tab"
        aria-selected={!isDark}
        onClick={() => flip("light")}
        title="Light mode"
        className={`w-7 h-7 flex items-center justify-center transition-colors ${
          !isDark
            ? "bg-zinc-800 text-zinc-100 shadow-sm"
            : "text-zinc-500 hover:text-zinc-200"
        }`}
      >
        <Sun className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}
