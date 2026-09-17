#!/usr/bin/env node
/**
 * Guards the one way the Korean-keyed translation table can rot silently.
 *
 * `translate()` looks a key up by exact string match and falls back to the key
 * itself, so a key that no longer matches any source literal does not throw,
 * does not fail tsc and does not fail the build. It just quietly stops
 * translating, and only a Korean-reading operator on an English UI would spot
 * it. This script is the thing that fails instead.
 *
 * Run: node tools/i18n-check.mjs   (or: npm run i18n:check)
 *
 * It does NOT require every Korean string to be translated. Partial coverage
 * is the design.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "src");
const MESSAGES = join(SRC, "lib", "messages", "en.ts");
// The second half of the same table, split off because the orphan check below
// is exactly wrong for it: its keys are Python literals in api/,
// data-pipeline/ and mcp-servers/, so none of them appears under src/ and all
// of them would read as orphans. tests/unit/test_en_server_keys.py runs the
// mirror-image check where those strings actually live. Both files are kept
// out of `walk`, so neither can rescue an orphan in the other.
const SERVER_MESSAGES = join(SRC, "lib", "messages", "en-server.ts");

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (
      /\.(ts|tsx)$/.test(name) &&
      p !== MESSAGES &&
      p !== SERVER_MESSAGES
    )
      out.push(p);
  }
  return out;
}

// Keys, straight off the module. Reading the object literal's keys with a
// regex would need to handle quoted, bare and multi-line forms; importing the
// built module from a .ts file needs a loader. Stripping comments and matching
// the key position is the middle ground, and the assertion below catches a
// parse that came back empty.
function parseKeys(file) {
  const raw = readFileSync(file, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  // Any indentation, not exactly two spaces: prettier moves a long key onto its
  // own line with deeper indentation, and the old two-space anchor silently
  // skipped 6 real keys, which were precisely the long ones most likely to drift.
  // Single quotes too, which prettier uses for a key containing a double quote.
  return [
    ...raw.matchAll(
      /^\s+(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|([^\s:"']+)):/gm,
    ),
  ].map((m) =>
    m[1] !== undefined
      ? m[1].replace(/\\"/g, '"')
      : m[2] !== undefined
        ? m[2].replace(/\\'/g, "'")
        : m[3],
  );
}

const keys = parseKeys(MESSAGES);
const serverKeys = parseKeys(SERVER_MESSAGES);

if (keys.length === 0) {
  console.error("i18n-check: parsed 0 keys out of en.ts. The parser is wrong.");
  process.exit(1);
}
if (serverKeys.length === 0) {
  console.error(
    "i18n-check: parsed 0 keys out of en-server.ts. The parser is wrong.",
  );
  process.exit(1);
}

const sources = walk(SRC).map((p) => readFileSync(p, "utf8"));
const haystack = sources.join("\n");

// Server keys are NOT orphan-checked here (see SERVER_MESSAGES above), but
// they are duplicate-checked, within their own file and against en.ts. `EN`
// spreads EN_SERVER first, so a cross-file collision silently shadows the
// server translation with the frontend one, and nothing else would say so.
const orphans = keys.filter((k) => !haystack.includes(k));
const allKeys = [...keys, ...serverKeys];
const dupes = allKeys.filter((k, i) => allKeys.indexOf(k) !== i);

let failed = false;
if (dupes.length) {
  failed = true;
  console.error(
    `i18n-check: ${dupes.length} duplicate key(s) across en.ts and` +
      " en-server.ts (en.ts wins, so a server translation is shadowed):",
  );
  for (const d of new Set(dupes)) console.error("  " + d);
}
if (orphans.length) {
  failed = true;
  console.error(
    `i18n-check: ${orphans.length} key(s) in en.ts match no string in src/.` +
      " Either the source string changed (update the key) or the UI is gone" +
      " (drop the entry):",
  );
  for (const o of orphans) console.error("  " + o);
}
// ---------------------------------------------------------------------------
// THE HOLE THIS GATE USED TO HAVE
//
// Everything above checks ONE direction: an en.ts key still matches something
// in src. It cannot see the opposite failure, and that is the one that reaches
// a user. Measured 2026-09-17: 12 Korean strings rendered to an English
// operator with tsc, the build and this check all green, among them the SIX
// dashboard tab labels on the most-visited screen and the Tasks scope filter.
//
// They share one shape. The literal lives in a module-level constant and
// reaches the UI through `t(<expression>)`:
//
//   const TAB_DEFS = [{ key: "overview", label: "개요" }, ...];
//   ...
//   {t(d.label)}
//
// The wrap is correct, so nothing looks wrong. But a scan for `t("...")`
// literals never sees "개요", so no key is added and translate() falls back to
// the Korean. The orphan test above cannot help: it only asks whether a key is
// still used, never whether a rendered string has one.
//
// So any Korean literal in a display-ish property of a file that uses t() must
// have a key. IGNORE holds the documented exceptions, each Korean ON PURPOSE.
const DISPLAY_PROP =
  /(?:label|hint|title|description|text|summary|name|eyebrow|placeholder|allLabel|tooltip)\s*:\s*"((?:[^"\\\n]|\\.)*)"/g;
const HANGUL = /[가-힣]/;

// Korean that is LOGIC or a model prompt, never display. Keep this short and
// say why: every entry is a hole in the gate.
const IGNORE = new Set([
  // Reserved for a literal that must stay unwrapped because it is compared.
  // engine-config-panel.tsx:39 is the live example: it produces
  // { text: "활성" } and :144 compares stream.text === "활성". That one needs no
  // entry here only because a 활성 key exists for the other, wrapped sites.
]);

const keySet = new Set(allKeys);
const unkeyed = [];
for (const file of walk(SRC)) {
  const body = readFileSync(file, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  if (!/\bt\(/.test(body)) continue; // no translator in the file, nothing to render
  for (const m of body.matchAll(DISPLAY_PROP)) {
    const lit = m[1];
    if (!HANGUL.test(lit)) continue;
    if (keySet.has(lit) || IGNORE.has(lit)) continue;
    unkeyed.push(`${file.replace(SRC + "/", "")}: ${lit}`);
  }
}
// THE OTHER HALF OF THE SAME HOLE, and the more common one.
//
// The block above catches a Korean literal that reaches the UI through
// t(<expression>). It does NOT catch the direct form, t("한글 문자열"), because
// that literal is not in a display-shaped property. Measured 2026-09-17: two
// strings added to /settings in this very session were wrapped in t() with no
// en.ts key, and this check still printed ok. A missing key renders the Korean
// to an English operator with tsc, the build and this gate all green, which is
// the failure this file exists to prevent.
//
// Matched on the ARGUMENT of a t()/tr() call that is a plain string literal.
// A t(expr) call is handled above; a template literal cannot be a key at all,
// because the table is looked up by exact equality, so one is reported too.
const T_LITERAL =
  /\bt(?:r)?\(\s*(?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)')/g;
const T_TEMPLATE = /\bt(?:r)?\(\s*`([^`]*[가-힣][^`]*)`/g;
const unkeyedDirect = [];
const templated = [];
for (const file of walk(SRC)) {
  const body = readFileSync(file, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  const rel = file.replace(SRC + "/", "");
  for (const m of body.matchAll(T_LITERAL)) {
    const lit = (m[1] !== undefined ? m[1] : m[2]).replace(/\\"/g, '"');
    if (!HANGUL.test(lit)) continue;
    if (keySet.has(lit) || IGNORE.has(lit)) continue;
    unkeyedDirect.push(`${rel}: ${lit.slice(0, 70)}`);
  }
  for (const m of body.matchAll(T_TEMPLATE)) {
    templated.push(`${rel}: ${m[1].slice(0, 70)}`);
  }
}
if (unkeyedDirect.length) {
  failed = true;
  console.error(
    `i18n-check: ${unkeyedDirect.length} Korean literal(s) passed straight to` +
      " t() with no en.ts key, so they show Korean on an English browser:",
  );
  for (const u of unkeyedDirect) console.error("  " + u);
}
if (templated.length) {
  failed = true;
  console.error(
    `i18n-check: ${templated.length} Korean TEMPLATE literal(s) passed to t().` +
      " The table is looked up by exact equality, so an interpolated key can" +
      " never match: use a {n} placeholder in the key and .replace() at the" +
      " call site.",
  );
  for (const t of templated) console.error("  " + t);
}

if (unkeyed.length) {
  failed = true;
  console.error(
    `i18n-check: ${unkeyed.length} Korean string(s) render through t(<expr>)` +
      " with no en.ts key, so they show Korean on an English browser:",
  );
  for (const u of unkeyed) console.error("  " + u);
}

if (failed) process.exit(1);

console.log(
  `i18n-check: ${keys.length} en.ts keys, all present in src/, plus` +
    ` ${serverKeys.length} server-authored keys, and every t(<expr>)` +
    " constant has one. ok",
);
