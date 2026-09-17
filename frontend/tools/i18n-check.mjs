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
/**
 * Comments removed, TRAILING ones included. The rules below read Korean as
 * evidence, so a commented-out display prop or an old t() call in a trailing
 * comment gets reported as a live missing key. Measured 2026-09-17: a trailing
 * `// label: "조회 전용 역할" 이었다가 짧아졌다` produced two failures for
 * strings that do not exist at runtime.
 *
 * Quote-aware: a blunt //.*$ cuts `https://` inside a string and swallows the
 * rest of that line, which silently DROPS code from every rule.
 */
function stripComments(src) {
  let out = "";
  for (const line of src.replace(/\/\*[\s\S]*?\*\//g, "").split("\n")) {
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
// The prop NAME is captured too (group 1, so the literal is group 2): the wrap
// check below needs to know which property to follow to its render site.
// All THREE quote styles. A double-quote-only match was measured blind in both
// directions at once: backticking the three `hint:` values in
// redundant-indexes-panel.tsx hid them from the missing-key check AND stopped
// the table being discovered, so unwrapping its Korean tooltip also went
// unreported. Backticks survive prettier, so this is a one-keystroke hole.
const DISPLAY_PROP = new RegExp(
  "(label|hint|title|description|text|summary|name|eyebrow|placeholder" +
    "|allLabel|tooltip)\\s*:\\s*" +
    '(?:"((?:[^"\\\\\\n]|\\\\.)*)"' +
    "|'((?:[^'\\\\\\n]|\\\\.)*)'" +
    "|`((?:[^`\\\\]|\\\\.)*)`)",
  "g",
);
const HANGUL = /[가-힣]/;

// Korean that is LOGIC or a model prompt, never display. Keep this short and
// say why: every entry is a hole in the gate.
const IGNORE = new Set([
  // Reserved for a literal that must stay unwrapped because it is compared.
  // engine-config-panel.tsx:39 is the live example: it produces
  // { text: "활성" } and :144 compares stream.text === "활성". That one needs no
  // entry here only because a 활성 key exists for the other, wrapped sites.
]);

// ---------------------------------------------------------------------------
// THE THIRD DIRECTION, which having a key does not cover: the render site
// dropped its t().
//
// Measured 2026-09-17: changing `{t(PRESETS[presetIdx].text)}` to
// `{PRESETS[presetIdx].text}` in query-lab, ONE character, put the Korean
// preset prompt back in front of an English operator, and this gate still
// printed ok. Neither existing rule can see it. The orphan rule asks whether a
// key is still used, and it still is (the table's own literal matches it). The
// DISPLAY_PROP rule asks whether a Korean literal HAS a key, and it does.
//
// So: for a constant table whose display prop holds Korean, every JSX CHILD
// expression that reads that prop must be wrapped in t().
//
// Scoped to the TABLE, not to the prop name, and that is load-bearing:
// dashboard/page.tsx has `label` on both RANGES (["1h","6h",...]) and TAB_DEFS
// (["개요",...]), so a name-scoped rule fires on `{r.label}` over the ASCII
// table, which is correct code, and a gate that fires on correct code gets
// deleted. The roots followed are the const itself (`PRESETS[i].text`) and the
// parameter of a `.map()` over it (`TAB_DEFS.map((d) => ... t(d.label))`).
// Attribute and template positions are skipped on purpose: `key={p.label}` is
// a React key and `${urgency.text}` is a Tailwind class, neither renders.
// Measured: zero hits across src/ as it stands.
//
// ponytail: the owning table is "nearest preceding const", which misattributes
// a Korean literal in an inline object to whatever const came before it. That
// costs coverage on those (no false positives, just no rule), never
// correctness. Bind it to a real parse only if a miss actually reaches a user.
//
// KNOWN CEILING, deliberately NOT gated: nothing asserts that a SERVER-authored
// key (en-server.ts) is applied at some render site. The one live instance was
// the config rejection message, fixed by hand to `t(saveError)` in
// settings/page.tsx. A general rule would have to trace a runtime string from a
// Lambda response to the JSX node that prints it, which no regex here can do.
function owningConst(body, idx) {
  const seen = [
    ...body.slice(0, idx).matchAll(/\bconst\s+([A-Za-z_$][\w$]*)/g),
  ];
  return seen.length ? seen[seen.length - 1][1] : null;
}

const keySet = new Set(allKeys);
const unkeyed = [];
const unwrapped = [];
for (const file of walk(SRC)) {
  const body = stripComments(readFileSync(file, "utf8"));
  if (!/\bt\(/.test(body)) continue; // no translator in the file, nothing to render
  const rel = file.replace(SRC + "/", "");
  const tables = new Map(); // const name -> display props holding Korean
  for (const m of body.matchAll(DISPLAY_PROP)) {
    const prop = m[1];
    const lit = m[2] ?? m[3] ?? m[4];
    if (lit === undefined) continue;
    if (!HANGUL.test(lit)) continue;
    if (!(keySet.has(lit) || IGNORE.has(lit))) {
      unkeyed.push(`${rel}: ${lit}`);
    }
    const owner = owningConst(body, m.index);
    if (!owner) continue;
    if (!tables.has(owner)) tables.set(owner, new Set());
    tables.get(owner).add(prop);
  }
  // A LIVE THIRD SHAPE, outside the display-prop family: a module-level
  // `const X = "<Korean>"` rendered as `{t(X)}`. schema-changes-panel.tsx does
  // this twice (UNKNOWN_ROWS, UNKNOWN_TYPE). Measured 2026-09-17: dropping the
  // t() at its render site, and adding a new such const with no key, BOTH left
  // this gate printing ok.
  //
  // The key is demanded only when the const is actually RENDERED, which is
  // what keeps a Korean LOGIC const out of it: rca-link.ts's RCA_PROMPT is a
  // module-level Korean string that no JSX prints, and it must not be asked
  // for a translation.
  const KO_CONST =
    /\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`)\s*;/g;
  for (const m of body.matchAll(KO_CONST)) {
    const name = m[1];
    const lit = m[2] ?? m[3] ?? m[4];
    if (lit === undefined || !HANGUL.test(lit)) continue;
    const wrapped = new RegExp(`\\bt\\(\\s*${name}\\s*\\)`).test(body);
    const bare = new RegExp(
      `(?<![$=\\w])\\{\\s*${name}\\s*\\}` +
        `|\\b(?:title|placeholder|aria-label|alt)=\\{\\s*${name}\\s*\\}`,
      "g",
    );
    const bareHits = [...body.matchAll(bare)];
    if (!wrapped && bareHits.length === 0) continue; // never rendered: logic
    if (!(keySet.has(lit) || IGNORE.has(lit))) unkeyed.push(`${rel}: ${lit}`);
    for (const b of bareHits) {
      unwrapped.push(`${rel}: ${b[0].trim()} (${name} is Korean)`);
    }
  }

  // A root is a bare identifier (a .map() parameter or a const alias), so the
  // same name can belong to two tables in one file. Measured as a FALSE
  // POSITIVE on correct code: renaming the unrelated `RANGES.map((r)` parameter
  // to `d` in dashboard/page.tsx, a pure rename with no behaviour change, made
  // this rule report `{d.label}` against TAB_DEFS.
  //
  // The alias arm only accepts an UPPER_SNAKE right-hand side, this repo's
  // convention for a const table. Without that, `const d = new Date()`
  // registered `d` as coming from `new`, every root looked ambiguous and the
  // rule silently switched itself off: the real tab-label defect stopped being
  // caught. A guard that disables the rule is worse than the false positive it
  // was added for.
  //
  // The binding census is taken over EVERY table in the file, not just the ones
  // holding Korean. That is the part a first attempt got wrong: `RANGES` has no
  // Korean in it, so it was absent from `tables`, `d` looked unambiguous and
  // the rule fired anyway. An ambiguous name is skipped rather than reported,
  // which costs coverage on shadowed names and never accuses correct code.
  const BINDINGS = new RegExp(
    `\\b([A-Za-z_$][\\w$]*)\\s*` +
      `(?:\\.\\s*[A-Za-z_$][\\w$]*\\s*\\((?:[^()]|\\([^()]*\\))*\\)\\s*)*` +
      `\\.\\s*(?:map|flatMap|forEach|filter|find)\\(\\s*\\(?\\s*([A-Za-z_$][\\w$]*)` +
      `|\\bconst\\s+([A-Za-z_$][\\w$]*)\\s*=\\s*([A-Z][A-Z0-9_]*)\\b`,
    "g",
  );
  const nameOwners = new Map(); // bound name -> set of tables it can come from
  for (const m of body.matchAll(BINDINGS)) {
    const [name, from] = m[2] ? [m[2], m[1]] : [m[3], m[4]];
    if (!name || !from) continue;
    if (!nameOwners.has(name)) nameOwners.set(name, new Set());
    nameOwners.get(name).add(from);
  }
  const rootsOf = new Map();
  for (const [owner, props] of tables) {
    const roots = new Set([owner]);
    for (const m of body.matchAll(
      new RegExp(
        // Chained calls are allowed between the owner and `.map(`, because
        // requiring a bare `OWNER.map(` missed the rule's OWN headline example:
        // dashboard/page.tsx:642 reads
        // `TAB_DEFS.filter((d) => visibleTabs.includes(d.key)).map((d) => (`,
        // so `d` never reached `roots`, and unwrapping `{t(d.label)}` at :652
        // put all six dashboard tab labels back in Korean with this gate
        // printing ok. Still ANCHORED to the owner identifier, and each
        // intervening call's argument list is matched with one level of
        // parenthesis nesting, so it cannot drift onto an unrelated `.map(`.
        // A chain whose argument nests two levels deep is not matched: that
        // costs coverage, never correctness.
        `\\b${owner}\\s*(?:\\.\\s*[A-Za-z_$][\\w$]*\\s*\\((?:[^()]|\\([^()]*\\))*\\)\\s*)*\\.\\s*map\\(\\s*\\(?\\s*([A-Za-z_$][\\w$]*)`,
        "g",
      ),
    )) {
      roots.add(m[1]);
    }
    // An indexed or plain ALIAS of the table is a root too:
    // redundant-indexes-panel.tsx:226 does
    // `const k = KIND_STYLES[candidate.kind];` and never maps, so without this
    // `title={k.hint}` on its Korean tooltip is invisible to the rule.
    for (const m of body.matchAll(
      new RegExp(`\\bconst\\s+([A-Za-z_$][\\w$]*)\\s*=\\s*${owner}\\b`, "g"),
    )) {
      roots.add(m[1]);
    }
    rootsOf.set(owner, roots);
  }
  for (const [owner, props] of tables) {
    for (const root of rootsOf.get(owner) ?? []) {
      const from = nameOwners.get(root);
      if (root !== owner && from && from.size > 1) continue; // ambiguous
      for (const prop of props) {
        // `{root[...].prop}` / `{root.a.prop}` in child position, or as the
        // value of a user-visible JSX attribute.
        //
        // Two positions render to the screen: a JSX text CHILD, and the value
        // of a user-visible ATTRIBUTE. The attribute side is an ALLOWLIST, not
        // an exclusion list: excluding `key=` and `ref=` and scanning
        // everything else was measured firing on correct code, because
        // `value={s.label}` on an <option> and `data-testid={s.label}` are
        // DATA, not display, and both were reported. `$` is excluded from the
        // child form because `${urgency.text}` is a template interpolation
        // building a Tailwind class, which renders nothing.
        const tail = `\\b(?:\\[[^\\]\\n]*\\]|\\.[A-Za-z_$][\\w$]*)*\\.${prop}\\s*\\}`;
        const re = new RegExp(
          `(?<![$=\\w])\\{\\s*${root}${tail}` +
            `|\\b(?:title|placeholder|aria-label|alt)=\\{\\s*${root}${tail}`,
          "g",
        );
        for (const m of body.matchAll(re)) {
          unwrapped.push(`${rel}: ${m[0].trim()} (${owner}.${prop} is Korean)`);
        }
      }
    }
  }
}
if (unwrapped.length) {
  failed = true;
  console.error(
    `i18n-check: ${unwrapped.length} render site(s) read a Korean display` +
      " prop without t(), so they show Korean on an English browser. The key" +
      " exists; the wrap is what is missing:",
  );
  for (const u of unwrapped) console.error("  " + u);
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
  const body = stripComments(readFileSync(file, "utf8"));
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
    " constant has one and is still wrapped at its render site. ok",
);
