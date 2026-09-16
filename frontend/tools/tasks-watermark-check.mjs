#!/usr/bin/env node
/**
 * Self-check for src/lib/tasks-watermark.ts, the module that decides what the
 * word "new" means on /fleet and /tasks. Get this wrong in either direction and
 * the operator either never sees an indicator or sees a permanent false one, so
 * the rules get an assertion each.
 *
 * Run: node tools/tasks-watermark-check.mjs
 * (Node strips the TypeScript annotations natively; there is no test runner in
 * this package, and the Playwright suite needs a live deployment.)
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";

// The module keys storage by Cognito identity, so it imports "@/lib/auth".
// Node knows nothing about the tsconfig path alias and that module pulls in the
// browser auth stack, so resolve it to a stub whose identity this script drives.
registerHooks({
  resolve(spec, ctx, next) {
    if (spec === "@/lib/auth") {
      return {
        url:
          "data:text/javascript," +
          encodeURIComponent(
            "export const getUsername = () => globalThis.__user ?? null;",
          ),
        shortCircuit: true,
      };
    }
    return next(spec, ctx);
  },
});

// localStorage shim: the module reads window.localStorage inside try/catch, so
// without this every call would take the "blocked storage" path and the
// monotonic-advance assertions below would prove nothing.
const store = new Map();
globalThis.window = {
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  },
};

const {
  readPublishedMark,
  advancePublishedMark,
  isNewPublication,
  countNewPublications,
  newestPublished,
} = await import("../src/lib/tasks-watermark.ts");

// First visit: no mark, and the whole backlog is NOT new.
assert.equal(readPublishedMark(), null);
assert.equal(isNewPublication("1757000000000", null), false);

// published_at is the only definition of new.
assert.equal(isNewPublication("1757000000001", "1757000000000"), true);
assert.equal(isNewPublication("1757000000000", "1757000000000"), false);
assert.equal(isNewPublication("1756999999999", "1757000000000"), false);
// Absent (pending / running / failed / legacy row) is never new.
assert.equal(isNewPublication(null, "1757000000000"), false);
assert.equal(isNewPublication(undefined, "1757000000000"), false);
assert.equal(isNewPublication("", "1757000000000"), false);
// A garbage value must not read as new either.
assert.equal(isNewPublication("not-a-number", "1757000000000"), false);

// Advancing is monotonic: an older or unparseable mark cannot resurrect reports
// the operator already read.
advancePublishedMark("1757000000000");
assert.equal(readPublishedMark(), "1757000000000");
advancePublishedMark("1756000000000");
assert.equal(readPublishedMark(), "1757000000000");
advancePublishedMark(null);
assert.equal(readPublishedMark(), "1757000000000");
advancePublishedMark("oops");
assert.equal(readPublishedMark(), "1757000000000");
advancePublishedMark("1757000000001");
assert.equal(readPublishedMark(), "1757000000001");

// Counting over a page of rows, and picking the newest PUBLICATION. The rows
// are ordered newest-first by created_at, so the newest publication is not
// simply rows[0]: a slow task queued earlier can publish after a fast one.
const rows = [
  { task_id: "queued-last", published_at: null }, // still running
  { task_id: "fast", published_at: "1757000000500" },
  { task_id: "slow", published_at: "1757000009000" }, // queued earlier, published later
  { task_id: "old", published_at: "1700000000000" },
  { task_id: "legacy" }, // no published_at at all
];
assert.equal(countNewPublications(rows, "1757000000000"), 2);
assert.equal(countNewPublications(rows, null), 0); // first visit
assert.equal(newestPublished(rows).task_id, "slow");
assert.equal(newestPublished([]), null);
assert.equal(newestPublished([{ task_id: "p", published_at: null }]), null);

// Per identity: one operator's reading position must not become another's, and
// since the visible cluster set is resolved from that same identity, the key
// also keeps a wider fleet's mark out of a narrower one.
globalThis.__user = "alice";
advancePublishedMark("1800000000000");
assert.equal(readPublishedMark(), "1800000000000");
globalThis.__user = "bob";
assert.equal(readPublishedMark(), null); // bob starts at "첫 방문"
advancePublishedMark("1750000000000");
assert.equal(readPublishedMark(), "1750000000000");
globalThis.__user = "alice";
assert.equal(readPublishedMark(), "1800000000000"); // bob did not move alice

console.log("tasks-watermark-check: ok");
