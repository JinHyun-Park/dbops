#!/usr/bin/env node
/**
 * Every prompt this app sends to the model must ask for its answer in the
 * language the operator's console is in, and it must ask through ONE helper.
 * Anything a prompt PRESCRIBES AS OUTPUT has to follow that same answer
 * language, through the second helper.
 *
 * Why this file exists rather than a type or a lint rule: a prompt with
 * `**한국어로**` baked back into it compiles, passes eslint, passes
 * `npm run build` and passes i18n-check (the string is a model instruction,
 * not a display prop, so nothing looks for a translation). The only visible
 * symptom is an English-reading DBA getting a Korean answer, which is exactly
 * the bug this replaced. The prescribed-output class is worse: the directive
 * flips, the answer comes back in English, and the headings inside it are
 * still Korean. So the gate has to be a direct assertion.
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

const { answerIn, labelIn } = await import("../src/lib/prompt-lang.ts");

// ---------------------------------------------------------------------------
// 1. The helpers say the right thing in each language.
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
// what every caller got before a locale existed. labelIn() shares that one
// normalisation, so the prescribed labels can never disagree with the
// directive about the locale: same inputs, same verdict.
for (const v of ["en-US", "EN", " en ", "en_GB"]) {
  assert.equal(answerIn(v), en, `"${v}" should read as English`);
  assert.equal(
    labelIn(v, "권장 조치", "Recommended action"),
    "Recommended action",
  );
}
for (const v of ["ko", "ko-KR", "fr", "", "  ", null, undefined, 7, {}, []]) {
  assert.equal(
    answerIn(v),
    ko,
    `${JSON.stringify(v)} should fail safe to Korean`,
  );
  assert.equal(
    labelIn(v, "권장 조치", "Recommended action"),
    "권장 조치",
    `${JSON.stringify(v)} should fail safe to the Korean label`,
  );
}

// ---------------------------------------------------------------------------
// 2. Every prompt is accounted for, as PINNED or as DELEGATED.
//
// The table used to count `answerIn(` calls and call that "prompt sites", and
// the pass message claimed every prompt in these files routed through the
// helper. It did not: maintenance-health-panel.tsx builds TWO prompts and
// pins one. The second one is correct as it stands, because a prompt with no
// pin leaves the choice to the agent's own locale-aware system prompt, which
// is the right outcome for a prompt the operator goes on talking to in /chat.
// So the count is per CLASS now:
//
//   pinned    = answerIn() calls. Verified mechanically.
//   delegated = prompts that deliberately pin nothing, each carrying a
//               `prompt-lang: delegated` comment at its builder. Also verified
//               mechanically, against that marker, so "delegated" is a fact in
//               the source and not a number claimed here.
//
// Either number moving means a prompt was added, or stopped routing through
// the helper, or lost its marker. All three are the moment to ask which class
// the prompt belongs in.
//
// `labels` is the third number, and it is reported rather than pinned: the
// prescribed literals are checked by NAME against PRESCRIBED_LABELS below, not
// by count, because a count is propped back up by any decoy call and was
// measured being propped up exactly that way.
//

// Every labelIn() call's ko/en pair, prettier's line wrapping tolerated.
const LABEL_PAIR =
  /labelIn\(\s*locale\s*,\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*,?\s*\)/gs;

/**
 * THE INVENTORY of prescribed output text: every literal a prompt dictates
 * that the ANSWER must contain, pinned by its exact ko/en pair.
 *
 * WHY A LIST AND NOT A RULE. Four shape rules were tried here first, one per
 * observed spelling (a numbered heading, a quoted answer string, a bold
 * heading followed by a colon, a markdown table row). An adversarial pass
 * measured them and they failed in BOTH directions at once:
 *
 *  - Leaky. Seven ordinary respellings of the same defect walked past all of
 *    them: `4) 추가 확인 사항` (a paren, not a period), `- **권장 조치** -`
 *    (a dash, not a colon), a full-width colon U+FF1A, `### 권장 조치`,
 *    `첫째, ...` with no number at all, `"조치 불필요"라고 응답해줘` (응답,
 *    not 답), and splitting a table row so no single literal held both Korean
 *    and two pipes.
 *  - Noisy. They fired on correct code: `**주의**:` is ordinary instruction
 *    prose, `1. 지표 스냅샷, 2. 최근 이벤트 로그` enumerates the model's INPUT,
 *    `2026. 9. 17.` is the Korean date format, `/ERROR|FATAL|PANIC/` and
 *    `OK | WARN | CRIT` are a regex and an enum, and `"모르겠다"라고 답한
 *    경우에는` describes what the OPERATOR said.
 *
 * "Prescribes output" is a semantic property of natural-language text, and no
 * regex decides it. So this gate stops guessing and pins the answer instead.
 * Set equality both ways: a literal that regressed to hardcoded Korean goes
 * MISSING whatever spelling it regressed to, and a new prescribed literal is
 * EXTRA until a human adds it here, which is the same contract the answerIn()
 * counts below already use.
 *
 * ponytail: an inventory, not an analysis. Two ceilings, both stated rather
 * than papered over with a rule that fires on correct code:
 *  - A NEW prescribed literal, hardcoded in Korean at a site that never had
 *    one, is invisible here. No mechanism was added for it, because the only
 *    ones available are the shape rules that were just measured leaking and
 *    misfiring, or a count of Korean literals per file, which moves on every
 *    unrelated UI string in the same component and so gets switched off. The
 *    review of a prompt edit is what catches this class today.
 *  - A decoy call carrying the exact missing pair would satisfy the set. That
 *    is sabotage rather than regression, and a reviewer reading that line sees
 *    it. Measured: the decoy that defeated the old COUNT does not defeat this,
 *    because the pair has to match, `labelIn("ko", ...)` is not `locale`, and
 *    rewording one side reports both a missing and an extra pair.
 * Upgrade path if the list gets long enough to rot: have each prompt builder
 * return its prescribed labels as data and assert on that, instead of scanning
 * source.
 */
const PRESCRIBED_LABELS = {
  "app/query-lab/page.tsx": [
    ["앞 80자", "first 80 chars"],
    ["재작성된 SQL", "Rewritten SQL"],
    ["변경 근거", "Rationale"],
    ["주의사항", "Caveats"],
  ],
  "components/dashboard/anomalies-panel.tsx": [
    ["추정 원인", "Likely cause"],
    ["운영 영향", "Operational impact"],
    ["다음 점검 단계", "Next check step"],
  ],
  "components/dashboard/event-detail-modal.tsx": [
    ["무슨 일이 일어났는지", "What happened"],
    ["영향", "Impact"],
    ["권장 조치", "Recommended action"],
    ["조치 불필요", "No action needed"],
  ],
  "components/dashboard/maintenance-health-panel.tsx": [
    ["왜 중요한지", "Why it matters"],
    ["구체적 조치", "Concrete action"],
    ["검증 방법", "How to verify"],
  ],
};
const DELEGATED_MARK = /prompt-lang:\s*delegated/g;
const EXPECTED = {
  // 4 presets share one composer, 2 plan-insight, bulk review, analyze
  // fallback, rewrite. Labels: the bulk-review table header, 3 rewrite
  // section headings.
  "app/query-lab/page.tsx": { pinned: 6, delegated: 0, labels: 4 },
  "components/dashboard/anomalies-panel.tsx": {
    pinned: 1,
    delegated: 0,
    labels: 3,
  },
  // 3 section headings plus the fixed no-impact answer string.
  "components/dashboard/event-detail-modal.tsx": {
    pinned: 1,
    delegated: 0,
    labels: 4,
  },
  // The AI explanation is pinned; proceedInChat() delegates.
  "components/dashboard/maintenance-health-panel.tsx": {
    pinned: 1,
    delegated: 1,
    labels: 3,
  },
  // These two prescribe no output text of their own: nothing to route.
  "components/dashboard/query-detail-modal.tsx": {
    pinned: 1,
    delegated: 0,
    labels: 0,
  },
  // Conversation title, follow-up question chips.
  "components/chat/chat-panel.tsx": { pinned: 2, delegated: 0, labels: 0 },
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

/** Comments stripped: a comment may legitimately discuss the directive, and
 *  three rules below read Korean as evidence, so a comment left in the body is
 *  a false positive waiting to happen. TRAILING comments are stripped too: a
 *  correct sender whose only Korean outside its prompt was the trailing
 *  `// answerIn이 한국어로 고정하지 않는다` tripped two rules at once.
 *
 *  Quote-aware on purpose. A blunt /\/\/.*$/ would cut `https://` inside a
 *  string and swallow the rest of that line, which silently DROPS code from
 *  every rule below. */
function code(path) {
  const src = readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  let out = "";
  for (const line of src.split("\n")) {
    let q = null;
    let cut = -1;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (q) {
        if (c === "\\") i++;
        else if (c === q) q = null;
        continue;
      }
      if (c === '"' || c === "'" || c === "`") q = c;
      else if (c === "/" && line[i + 1] === "/") {
        cut = i;
        break;
      }
    }
    out += (cut < 0 ? line : line.slice(0, cut)) + "\n";
  }
  return out;
}

/** Markdown emphasis dropped, so `**한국어**로` reads as `한국어로`. That
 *  spelling is what this round removed from query-lab, and it walked straight
 *  past a contiguous match. */
function plain(body) {
  return body.replace(/\*/g, "");
}

const files = walk(SRC);
let failed = false;
const fail = (msg) => {
  failed = true;
  console.error("prompt-lang-check: " + msg);
};

let pinnedTotal = 0;
let delegatedTotal = 0;
let labelTotal = 0;
for (const [rel, want] of Object.entries(EXPECTED)) {
  const path = join(SRC, rel);
  const raw = readFileSync(path, "utf8");
  const body = code(path);
  const pinned = (body.match(/answerIn\(/g) ?? []).length;
  const want_labels = PRESCRIBED_LABELS[rel] ?? [];
  const got_labels = [...body.matchAll(LABEL_PAIR)].map((m) => [m[1], m[2]]);
  const labels = got_labels.length;
  // Counted in the RAW file: the marker is a comment, which code() strips.
  const delegated = (raw.match(DELEGATED_MARK) ?? []).length;
  pinnedTotal += pinned;
  delegatedTotal += delegated;
  labelTotal += labels;
  // Set equality both ways against PRESCRIBED_LABELS, which is why this does
  // not need to recognise the SHAPE of the literal it is protecting.
  const key = (pair) => pair.join(" -> ");
  const gotSet = new Set(got_labels.map(key));
  const wantSet = new Set(want_labels.map(key));
  for (const k of wantSet) {
    if (!gotSet.has(k)) {
      fail(
        `${rel} no longer routes the prescribed literal \`${k}\` through` +
          " labelIn(). A prompt that dictates output text has to dictate it" +
          " in the ANSWER's language, or an English answer comes back with" +
          " Korean labels in it. If the wording changed on purpose, update" +
          " PRESCRIBED_LABELS in this file.",
      );
    }
  }
  for (const k of gotSet) {
    if (!wantSet.has(k)) {
      fail(
        `${rel} routes \`${k}\` through labelIn(), which is not in` +
          " PRESCRIBED_LABELS. A new prescribed literal is deliberate, so" +
          " add it to the inventory in this file.",
      );
    }
  }
  if (pinned !== want.pinned) {
    fail(
      `${rel} pins ${pinned} prompt(s) via answerIn(), expected` +
        ` ${want.pinned}. FEWER means a prompt stopped routing through the` +
        " helper and has a hardcoded answer language. MORE means a prompt was" +
        " added, or one builder was split into several: nothing is broken," +
        " but bump the count here deliberately so the next move is still" +
        " visible.",
    );
  }
  if (delegated !== want.delegated) {
    fail(
      `${rel} marks ${delegated} prompt(s) as \`prompt-lang: delegated\`,` +
        ` expected ${want.delegated}. A prompt that pins nothing must say so` +
        " at its builder, or it cannot be told apart from one that forgot.",
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
// covered the day it lands. A prompt reaches the model through streamChat(),
// as a /chat?prompt= deep link built by hand, or through URLSearchParams:
// rca-link.ts uses the last form, so this rule used to skip that file whole.
// (Its prompt pins nothing, which is intended there too, the agent decides.
// The point is that the gate can SEE the file.)
// `.append(` is behaviour-identical to `.set(` for one key, and swapping them
// dropped rca-link.ts out of discovery entirely (measured). Both, plus any
// other URLSearchParams method, are matched by name.
const SENDS_PROMPT =
  /streamChat\(|prompt=\$\{encodeURIComponent|\.\w+\(\s*["']prompt["']/;
const senders = files.filter((p) => SENDS_PROMPT.test(code(p)));
// A rule that matches nothing passes for free, and this one is discovery-based,
// so it has to prove it still discovers.
assert.ok(
  senders.length >= 7,
  `only ${senders.length} prompt-sending file(s) found; the matcher is wrong`,
);
for (const path of senders) {
  if (/한국어/.test(plain(code(path)))) {
    fail(
      `${relative(SRC, path)} sends a prompt and still names Korean in its` +
        " source. Use answerIn(locale) so the answer follows the console.",
    );
  }
}

// The helper itself must be the only place the wording lives.
const owners = files.filter(
  (p) => /한국어로/.test(plain(code(p))) && !p.endsWith("prompt-lang.ts"),
);
if (owners.length) {
  fail(
    "the Korean answer-language directive is duplicated outside" +
      " prompt-lang.ts: " +
      owners.map((p) => relative(SRC, p)).join(", "),
  );
}

// RULE 4 WAS DELETED, and the reason is recorded at PRESCRIBED_LABELS above:
// four regex shapes tried to decide whether Korean text "prescribes output",
// and an adversarial pass measured them leaking past seven ordinary
// respellings while firing on six pieces of correct code. That property is
// semantic, so the inventory decides it instead, by name.

if (failed) process.exit(1);

console.log(
  `prompt-lang-check: answerIn() and labelIn() fail safe to Korean;` +
    ` ${pinnedTotal} pinned + ${delegatedTotal} delegated =` +
    ` ${pinnedTotal + delegatedTotal} prompts across` +
    ` ${Object.keys(EXPECTED).length} files, all ${labelTotal} inventoried` +
    ` prescribed literals route through labelIn(), and ${senders.length}` +
    " prompt-sending files name no answer language of their own. ok",
);
