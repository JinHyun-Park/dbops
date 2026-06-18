// Per-cluster cache of the last COMPLETED RCA drawer analysis. The drawer's
// analysis is otherwise a throwaway stream, so leaving the drawer (without
// "전체 대화로 이어가기") discarded a long-running result. With this cache,
// reopening the drawer for the same cluster shows the previous analysis
// instantly — no re-run — with a "다시 실행" to refresh.
//
// Local-only (localStorage, this device), best-effort, and bounded to the most
// recent N clusters so it can't grow unbounded.

export interface RcaCacheEntry {
  analysis: string;
  tools: { name: string; status: string }[];
  ts: number; // epoch ms when the analysis completed
}

const KEY = "dbops_rca_cache_v1";
const MAX_ENTRIES = 15;

type CacheMap = Record<string, RcaCacheEntry>;

function readAll(): CacheMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as CacheMap) : {};
  } catch {
    return {};
  }
}

export function loadRcaCache(clusterId: string): RcaCacheEntry | null {
  if (!clusterId) return null;
  const entry = readAll()[clusterId];
  return entry && entry.analysis ? entry : null;
}

export function saveRcaCache(clusterId: string, entry: RcaCacheEntry): void {
  if (typeof window === "undefined" || !clusterId || !entry.analysis) return;
  try {
    const all = readAll();
    all[clusterId] = entry;
    // Bound size: drop the oldest beyond MAX_ENTRIES (by completion time).
    const ids = Object.keys(all);
    if (ids.length > MAX_ENTRIES) {
      ids
        .sort((a, b) => (all[a].ts || 0) - (all[b].ts || 0))
        .slice(0, ids.length - MAX_ENTRIES)
        .forEach((id) => delete all[id]);
    }
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    /* best-effort — quota / private mode */
  }
}
