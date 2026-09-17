/**
 * Which language a STORED RCA narrative is written in.
 *
 * The RCA inbox is fleet-wide, so two operators whose consoles are set to
 * different languages read the SAME stored report, and model prose is frozen in
 * whatever language generated it: it is not an i18n key, so no render site can
 * translate it afterwards (EN[...] would miss and hand back the input). The
 * reader has to be TOLD instead, and this is the one place that decides what to
 * tell them.
 *
 * `narrative_locale` is stamped on the result by task_worker._narrative for
 * every report generated since the prose started following the console locale.
 * Rows written before that carry no stamp, and every one of them is Korean,
 * because Korean is the only language that prompt could produce. So the script
 * of the prose is the fallback, and for exactly the rows that need it, the
 * unstamped ones, it is not a guess.
 *
 * ponytail: a script test, not a language detector, and only ever a fallback.
 * Ceiling: an English narrative quoting a Korean phrase would read as Korean.
 * That cannot happen on an unstamped row (none of them are English) and never
 * runs on a stamped one, and the cost of being wrong is one label, not a
 * decision. Upgrade path: delete the fallback once the last unstamped row has
 * aged past the agent-tasks TTL.
 */

/** Hangul syllables. Enough to tell the two console languages apart. */
const HANGUL = /[가-힣]/;

export type NarrativeLanguage = "ko" | "en";

/**
 * The language of `result.narrative`, or null when there is no narrative to
 * label. The stamp wins whenever it is one of the two languages the console
 * offers; anything else (absent, empty, a value from a future release) falls
 * through to the prose itself.
 */
export function narrativeLanguage(
  result:
    | { narrative?: unknown; narrative_locale?: unknown }
    | null
    | undefined,
): NarrativeLanguage | null {
  const prose = String(result?.narrative ?? "");
  if (!prose.trim()) return null;
  const stamped = String(result?.narrative_locale ?? "");
  if (stamped === "ko" || stamped === "en") return stamped;
  return HANGUL.test(prose) ? "ko" : "en";
}
