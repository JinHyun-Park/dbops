/**
 * The ONE place that decides how a model prompt asks for its answer language.
 *
 * Every prompt this app sends is code-authored Korean, and 14 of them used to
 * pin the answer with a literal `**한국어로**`. A user turn beats the system
 * prompt, so an operator reading an English console got a Korean answer even
 * though the agent already switches on its own (`answer_language()` in
 * `agent/prompts/system_prompt.py`, fed by the `locale` that
 * `agentcore-sse.ts` already puts in the invocation body). Those 14 sites
 * interpolate this instead, so the wording lives in one file rather than in 14
 * ternaries that drift.
 *
 * FAIL-SAFE TO KOREAN, the same rule the agent uses: anything that is not
 * exactly "en" answers in Korean, which is what every caller got before a
 * locale existed.
 *
 * Why not inside `i18n.tsx`: this module is asserted on under bare node by
 * `tools/prompt-lang-check.mjs`, which has no JSX or React loader. A plain
 * `.ts` file with a type-only import (erased by Node's type stripping) is
 * loadable as is.
 */
import type { Locale } from "./i18n";

/**
 * The answer-language directive.
 *
 * Shaped as the adverbial phrase the Korean prompts already used, so it drops
 * into every site in place and the Korean prompt text stays what shipped. The
 * instruction body around it stays Korean on purpose: it is tuned text, and
 * the model follows a Korean instruction to answer in English fine. That is
 * the same call `system_prompt.py` documents at its `answer_rule`, and
 * rewriting a tuned prompt would change the answer.
 */
export function answerIn(locale: Locale): string {
  // Normalised the same way `answer_language()` does, so a value that came
  // from somewhere other than `detectLocale()` (a stored "en-US", a stray
  // `navigator.language`) still reads as English instead of silently falling
  // back to Korean. Anything else, including null and undefined at runtime,
  // is Korean.
  const isEnglish =
    typeof locale === "string" && locale.trim().toLowerCase().startsWith("en");
  return isEnglish ? "**in English**" : "**한국어로**";
}
