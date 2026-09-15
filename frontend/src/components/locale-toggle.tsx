"use client";

import { useLocale } from "@/lib/i18n";

/**
 * Segmented KO / EN pill, deliberately built like ThemeToggle next to it:
 * same border, same 7x7 cells, same "current one is filled" read.
 *
 * ThemeToggle gates its icon on `mounted` because a static export renders
 * before the client knows which theme is stored. The same applies here, so
 * neither half is marked selected until `ready` says the client has resolved
 * the locale. Asserting one would flash the wrong language as the answer.
 */
export function LocaleToggle() {
  const { locale, setLocale, ready, t } = useLocale();

  const cell = (selected: boolean) =>
    `w-7 h-7 flex items-center justify-center text-[10px] font-medium tracking-wide transition-colors ${
      selected
        ? "bg-zinc-800 text-zinc-100 shadow-sm"
        : "text-zinc-500 hover:text-zinc-200"
    }`;

  return (
    <div
      role="tablist"
      aria-label={t("언어")}
      className="inline-flex items-center gap-0.5 border border-zinc-800 bg-zinc-900/50 p-0.5 rounded-md"
    >
      <button
        role="tab"
        aria-selected={ready ? locale === "ko" : undefined}
        onClick={() => setLocale("ko")}
        title={t("한국어")}
        className={cell(ready && locale === "ko")}
      >
        KO
      </button>
      <button
        role="tab"
        aria-selected={ready ? locale === "en" : undefined}
        onClick={() => setLocale("en")}
        title={t("영어")}
        className={cell(ready && locale === "en")}
      >
        EN
      </button>
    </div>
  );
}
