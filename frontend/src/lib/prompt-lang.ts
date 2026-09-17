/**
 * The ONE place that decides how a model prompt asks for its answer language,
 * and, for a prompt that prescribes its own output text, which language that
 * text is in. Both helpers share one locale test, so they cannot disagree.
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
  return isEnglish(locale) ? "**in English**" : "**한국어로**";
}

/**
 * A literal the prompt PRESCRIBES as output, in the answer's own language.
 *
 * A prompt that dictates output text (a markdown heading the answer must use,
 * a fixed string the model is told to reply with) has to dictate it in the
 * language the answer is in. Otherwise `answerIn()` flips the directive and
 * nothing else, and an English answer comes back with Korean labels embedded
 * in it: "3. **권장 조치**" over English prose, or the literal "조치 불필요"
 * as the one sentence a no-impact event returns.
 *
 * Only OUTPUT text goes through here. The instruction body around it stays
 * Korean on purpose, for the reason `answerIn()` documents above.
 */
export function labelIn(locale: Locale, ko: string, en: string): string {
  return isEnglish(locale) ? en : ko;
}

// Normalised the same way `answer_language()` does, so a value that came from
// somewhere other than `detectLocale()` (a stored "en-US", a stray
// `navigator.language`) still reads as English instead of silently falling back
// to Korean. Anything else, including null and undefined at runtime, is Korean:
// FAIL-SAFE TO KOREAN, one copy, shared by both helpers above so the directive
// and the labels it governs can never disagree about the locale.
function isEnglish(locale: Locale): boolean {
  return (
    typeof locale === "string" && locale.trim().toLowerCase().startsWith("en")
  );
}
