"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { EN } from "./messages/en";

export type Locale = "ko" | "en";

const STORAGE_KEY = "dbops_locale";

/**
 * KOREAN IS THE KEY, English is the translation.
 *
 * The app shipped Korean-only, so the alternative (rewrite every literal into an
 * English key and translate back to Korean) would have touched 2,566 strings across
 * 99 files with a real chance of changing what Korean users already see. Keying off
 * the existing Korean makes the conversion a pure `t(...)` wrap: the Korean output is
 * unchanged by construction, and English is a pure addition. A missing entry falls
 * back to the Korean source, which is visible but never blank.
 */
export function translate(locale: Locale, ko: string): string {
  if (locale === "ko") return ko;
  return EN[ko] ?? ko;
}

function readStored(): Locale | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "ko" || v === "en" ? v : null;
  } catch {
    return null; // private mode / blocked storage
  }
}

/** Stored choice wins; otherwise follow the browser. */
export function detectLocale(): Locale {
  const stored = readStored();
  if (stored) return stored;
  try {
    return navigator.language?.toLowerCase().startsWith("ko") ? "ko" : "en";
  } catch {
    return "ko";
  }
}

type Ctx = {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: (ko: string) => string;
  /** False until the client has resolved the locale. Use it to avoid asserting a
   *  language in the first paint, the same way ThemeToggle gates its icon. */
  ready: boolean;
};

const LocaleContext = createContext<Ctx>({
  locale: "ko",
  setLocale: () => {},
  t: (ko) => ko,
  ready: false,
});

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  // Starts at "ko" on purpose: this is a static export, so the prerendered HTML
  // contains the Korean source text. Resolving in an effect means an English user
  // sees one Korean paint before the swap. That is the same tradeoff the theme
  // toggle already accepts, and the alternative (blocking render) is worse.
  const [locale, setLocaleState] = useState<Locale>("ko");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const resolved = detectLocale();
    setLocaleState(resolved);
    document.documentElement.lang = resolved;
    setReady(true);
  }, []);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    document.documentElement.lang = next;
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* quota / blocked - the choice just will not persist */
    }
  }, []);

  const t = useCallback((ko: string) => translate(locale, ko), [locale]);

  const value = useMemo(
    () => ({ locale, setLocale, t, ready }),
    [locale, setLocale, t, ready],
  );

  return (
    <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
  );
}

export function useLocale(): Ctx {
  return useContext(LocaleContext);
}

/** Shorthand for the common case: `const t = useT();  t("느린 쿼리")`. */
export function useT(): (ko: string) => string {
  return useContext(LocaleContext).t;
}
