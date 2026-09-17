/**
 * Which language a STORED piece of model prose is written in: an RCA narrative
 * (`narrativeLanguage`) or an operations report summary (`summaryLanguage`).
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

/**
 * The language of a STORED operations report summary, or null when there is no
 * summary to label. Same problem as the RCA narrative on a fleet-wide screen,
 * and the same one Hangul test, but there is no stamp to prefer over it.
 *
 * ponytail: script test, no stamp, ON PURPOSE. `reports` has columns
 * (cluster_id, report_type, report_date, summary, data, s3_key) and no locale
 * column, and `schema_version` is a SHA-256 over the whole schema_migrator/sql
 * directory, so adding one re-fires the migrator Custom Resource for a label.
 * The script is sound for exactly this input: the summary is three to five
 * sentences of one language, everything the English directive preserves
 * verbatim is ASCII (metric names, parameter names, cluster IDs), and
 * _build_summary_prompt is assembled from aas / storage / connections numbers,
 * so no operator-entered Korean can reach it.
 * Ceiling: an English summary quoting a Korean phrase would read as Korean,
 * and the cost is one wrong label above prose the reader can see for
 * themselves. Upgrade path if that ever bites: stamp the row (a
 * summary_locale column plus a reader in api/reports) and prefer the stamp
 * here, the way narrativeLanguage already does.
 */
export function summaryLanguage(summary: unknown): NarrativeLanguage | null {
  const prose = String(summary ?? "");
  if (!prose.trim()) return null;
  return HANGUL.test(prose) ? "ko" : "en";
}
