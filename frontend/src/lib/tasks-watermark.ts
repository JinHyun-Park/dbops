/**
 * The /tasks inbox watermark: "which published reports had I already seen?"
 *
 * WHAT "NEW" MEANS HERE, and only this:
 *   a row whose `published_at` is non-null and strictly greater than the
 *   persisted mark. `published_at` is stamped when a READABLE report became
 *   available through the read API, so it is the only field that answers
 *   "is there something for me to go read".
 *
 * NEVER any of these:
 *   - `created_at` / `started_at`: a queued or running task has nothing to read.
 *   - a status flip: "done" is an internal transition, not a publication.
 *   - `anchor_time`: that is the INCIDENT clock, a different clock. An RCA
 *     published this morning about yesterday's event is new content about an
 *     old incident, and both times belong on the row.
 *   - "the top-ranked cause changed since the last report": the ranker's
 *     recency factor and peak path move the score with no change in the
 *     database, so that would manufacture novelty out of our own scoring.
 *
 * WHO MAY ADVANCE IT: only a successful load of the UNFILTERED inbox at
 * /tasks, with the server's `published_high_water_mark`. A `?cluster=` or
 * `?status=` page carries no fleet truth and must not advance it, and a
 * read-only surface that merely SHOWS the count (the /fleet summary) must
 * never advance it: if it did, opening /fleet would silently mark everything
 * read and the indicator would never appear again.
 *
 * LEGACY ROWS carry no `published_at`. Absent is not new, and neither is the
 * whole backlog on a first visit: with no mark persisted yet, nothing counts
 * as new and the UI says "첫 방문" instead of a number.
 */

import { getUsername } from "@/lib/auth";

const KEY_BASE = "dbops_tasks_published_mark";

/**
 * PER BROWSER, PER IDENTITY. The mark is one operator's reading position, so it
 * is keyed by the Cognito identity (`cognito:username`, falling back to `sub`).
 *
 * That key scopes the TENANT too, without a second field: the visible cluster
 * set is resolved server-side from this same identity's team membership, so the
 * mark is always a high-water mark over exactly the rows this identity may see.
 * Two operators sharing a browser therefore cannot inherit each other's
 * position, and a mark computed over a wider fleet cannot leak into a narrower
 * one by being stored under a shared key.
 *
 * A signed-out browser has no identity and falls back to the bare key. Harmless:
 * nothing can be fetched to compare against it until a token exists.
 */
function storageKey(): string {
  const id = getUsername();
  return id ? `${KEY_BASE}:${id}` : KEY_BASE;
}

/** ms-epoch decimal strings, the convention of every timestamp on the
 *  agent-tasks table. Compared as numbers so a legacy value of a different
 *  width cannot sort wrongly against a 13-digit one. */
function toMs(v: string | null | undefined): number | null {
  if (!v) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** The persisted mark, or null on a first visit (or blocked storage, which we
 *  treat as a first visit rather than as "everything is new"). */
export function readPublishedMark(): string | null {
  try {
    return window.localStorage.getItem(storageKey());
  } catch {
    return null;
  }
}

/**
 * Move the mark forward to the server's high-water mark. Monotonic: an older
 * or unparseable value is ignored, so an out-of-order response cannot resurrect
 * reports the operator already read.
 *
 * Call this ONLY after a successful unfiltered inbox load.
 */
export function advancePublishedMark(mark: string | null | undefined): void {
  const next = toMs(mark);
  if (next === null) return;
  const current = toMs(readPublishedMark());
  if (current !== null && current >= next) return;
  try {
    window.localStorage.setItem(storageKey(), String(next));
  } catch {
    /* private mode or quota: the mark just will not persist */
  }
}

export function isNewPublication(
  publishedAt: string | null | undefined,
  mark: string | null | undefined,
): boolean {
  const published = toMs(publishedAt);
  if (published === null) return false; // pending, running, failed, legacy
  const seen = toMs(mark);
  if (seen === null) return false; // first visit: the backlog is not "new"
  return published > seen;
}

/** Minimal shape both surfaces share; the full row type lives in api-client. */
interface Publishable {
  published_at?: string | null;
}

export function countNewPublications<T extends Publishable>(
  rows: T[],
  mark: string | null | undefined,
): number {
  return rows.reduce(
    (acc, r) => acc + (isNewPublication(r.published_at, mark) ? 1 : 0),
    0,
  );
}

/**
 * The most recently PUBLISHED row, which is not simply the first one: the list
 * is ordered newest-first by `created_at`, and a slow task queued earlier can
 * publish after a fast one queued later.
 */
export function newestPublished<T extends Publishable>(rows: T[]): T | null {
  let best: T | null = null;
  let bestMs = -1;
  for (const r of rows) {
    const ms = toMs(r.published_at);
    if (ms !== null && ms > bestMs) {
      best = r;
      bestMs = ms;
    }
  }
  return best;
}
