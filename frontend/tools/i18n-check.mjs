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

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name) && p !== MESSAGES) out.push(p);
  }
  return out;
}

// Keys, straight off the module. Reading the object literal's keys with a
// regex would need to handle quoted, bare and multi-line forms; importing the
// built module from a .ts file needs a loader. Stripping comments and matching
// the key position is the middle ground, and the assertion below catches a
// parse that came back empty.
const raw = readFileSync(MESSAGES, "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^\s*\/\/.*$/gm, "");
const keys = [
  ...raw.matchAll(/^\s{2}(?:"((?:[^"\\]|\\.)*)"|([^\s:"]+)):/gm),
].map((m) => (m[1] !== undefined ? m[1].replace(/\\"/g, '"') : m[2]));

if (keys.length === 0) {
  console.error("i18n-check: parsed 0 keys out of en.ts. The parser is wrong.");
  process.exit(1);
}

const sources = walk(SRC).map((p) => readFileSync(p, "utf8"));
const haystack = sources.join("\n");

const orphans = keys.filter((k) => !haystack.includes(k));
const dupes = keys.filter((k, i) => keys.indexOf(k) !== i);

let failed = false;
if (dupes.length) {
  failed = true;
  console.error(
    `i18n-check: ${dupes.length} duplicate key(s) in en.ts (the later one wins):`,
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
if (failed) process.exit(1);

console.log(`i18n-check: ${keys.length} keys, all present in src/. ok`);
