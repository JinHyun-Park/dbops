#!/usr/bin/env node
/**
 * Every prompt this app sends to the model must ask for its answer in the
 * language the operator's console is in, and it must ask through ONE helper.
 *
 * Why this file exists rather than a type or a lint rule: a prompt with
 * `**한국어로**` baked back into it compiles, passes eslint, passes
 * `npm run build` and passes i18n-check (the string is a model instruction,
 * not a display prop, so nothing looks for a translation). The only visible
 * symptom is an English-reading DBA getting a Korean answer, which is exactly
 * the bug this replaced. So the gate has to be a direct assertion.
 *
 * Run: node tools/prompt-lang-check.mjs   (or: npm run prompt:check)
 * (Node strips the TypeScript annotations natively; this package has no test
 * runner and the Playwright suite needs a live deployment.)
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, "..", "src");

const { answerIn } = await import("../src/lib/prompt-lang.ts");

// ---------------------------------------------------------------------------
// 1. The helper says the right thing in each language.
//
// Asserted by PROPERTY, not by exact bytes: the wording is tuned text and
// someone may well retune it, but a directive that names the wrong language,
// or names both, or names neither, is always a bug.
const HANGUL = /[가-힣]/;
const ko = answerIn("ko");
const en = answerIn("en");

assert.match(ko, /한국어/, "the ko directive must name Korean");
assert.doesNotMatch(
  ko,
  /English/i,
  "the ko directive must not also name English",
);
assert.match(en, /\bEnglish\b/, "the en directive must name English");
assert.doesNotMatch(
  en,
  HANGUL,
  "the en directive must be English, with no Korean left in it",
);
assert.notEqual(ko, en, "the two locales must not produce the same directive");
// Both are interpolated mid-sentence into a Korean prompt, so an empty or
// whitespace-only directive would leave the prompt silently unpinned.
assert.ok(ko.trim().length > 0 && en.trim().length > 0);

// FAIL-SAFE TO KOREAN, matching `answer_language()` on the agent side: only an
// explicitly English locale flips, everything else answers in Korean, which is
// what every caller got before a locale existed.
for (const v of ["en-US", "EN", " en ", "en_GB"]) {
  assert.equal(answerIn(v), en, `"${v}" should read as English`);
}
for (const v of ["ko", "ko-KR", "fr", "", "  ", null, undefined, 7, {}, []]) {
  assert.equal(
    answerIn(v),
    ko,
    `${JSON.stringify(v)} should fail safe to Korean`,
  );
}

// ---------------------------------------------------------------------------
// 2. Every prompt builder routes through the helper.
//
// The counts are per file and deliberate: adding a prompt means bumping a
// number here, which is the moment to ask whether the new prompt pins a
// language. Same trade the tool-schema parity test makes on the backend.
const EXPECTED = {
  "app/query-lab/page.tsx": 9, // 4 presets, 2 plan-insight, bulk review, analyze fallback, rewrite
  "components/dashboard/anomalies-panel.tsx": 1,
  "components/dashboard/event-detail-modal.tsx": 1,
  "components/dashboard/maintenance-health-panel.tsx": 1,
  "components/dashboard/query-detail-modal.tsx": 1,
  "components/chat/chat-panel.tsx": 1, // conversation title
};

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

/** Comments stripped: a comment may legitimately discuss the directive. */
function code(path) {
  return readFileSync(path, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

const files = walk(SRC);
let failed = false;
const fail = (msg) => {
  failed = true;
  console.error("prompt-lang-check: " + msg);
};

for (const [rel, want] of Object.entries(EXPECTED)) {
  const path = join(SRC, rel);
  const body = code(path);
  const got = (body.match(/answerIn\(/g) ?? []).length;
  if (got !== want) {
    fail(
      `${rel} calls answerIn() ${got} time(s), expected ${want}.` +
        " A prompt that stopped routing through the helper has a hardcoded" +
        " answer language; a new prompt needs the count bumped here.",
    );
  }
  if (!/from "@\/lib\/prompt-lang"/.test(body)) {
    fail(`${rel} does not import the helper from @/lib/prompt-lang`);
  }
}

// ---------------------------------------------------------------------------
// 3. No file that sends a prompt may name a language itself.
//
// Discovery-based rather than a fixed list, so a NEW prompt-sending file is
// covered the day it lands. A prompt reaches the model either through
// streamChat() or as a /chat?prompt= deep link.
const SENDS_PROMPT = /streamChat\(|prompt=\$\{encodeURIComponent/;
for (const path of files) {
  const body = code(path);
  if (!SENDS_PROMPT.test(body)) continue;
  if (/한국어/.test(body)) {
    fail(
      `${relative(SRC, path)} sends a prompt and still names Korean in its` +
        " source. Use answerIn(locale) so the answer follows the console.",
    );
  }
}

// The helper itself must be the only place the wording lives.
const owners = files.filter(
  (p) => /한국어로/.test(code(p)) && !p.endsWith("prompt-lang.ts"),
);
if (owners.length) {
  fail(
    "the Korean answer-language directive is duplicated outside" +
      " prompt-lang.ts: " +
      owners.map((p) => relative(SRC, p)).join(", "),
  );
}

if (failed) process.exit(1);

const total = Object.values(EXPECTED).reduce((a, b) => a + b, 0);
console.log(
  `prompt-lang-check: answerIn() fails safe to Korean, and all ${total}` +
    ` prompt sites across ${Object.keys(EXPECTED).length} files route` +
    " through it. ok",
);
