// Shared "currently selected cluster" store — the single source of truth that
// lets the ⌘K switcher, the header chip, and (Phase 2 Step B) every page agree
// on which cluster you're looking at, persisting across page navigations.
//
// Source of truth precedence: the URL `?cluster=` param wins (so deep links and
// Fleet "open dashboard" links are authoritative), falling back to the last
// choice in localStorage. Writes go to localStorage + broadcast a custom event
// so listeners re-render without a full reload. Deliberately framework-free
// (no useSearchParams) so it's safe under Next static export.

export interface ClusterLite {
  cluster_id: string;
  engine?: string;
  engine_version?: string;
}

const KEY = "dbops_selected_cluster";
export const CLUSTER_CHANGE_EVENT = "dbops:cluster-changed";

export function getSelectedCluster(): string | null {
  if (typeof window === "undefined") return null;
  const fromUrl = new URLSearchParams(window.location.search).get("cluster");
  if (fromUrl) return fromUrl;
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setSelectedCluster(id: string): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(KEY, id);
  } catch {
    /* ignore quota/private-mode errors — selection still works in-session */
  }
  window.dispatchEvent(new CustomEvent(CLUSTER_CHANGE_EVENT, { detail: id }));
}

// Subscribe to selection changes (custom event) AND browser back/forward
// (popstate), which can change the URL `?cluster=`. Returns an unsubscribe fn.
export function onClusterChange(cb: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(CLUSTER_CHANGE_EVENT, cb);
  window.addEventListener("popstate", cb);
  return () => {
    window.removeEventListener(CLUSTER_CHANGE_EVENT, cb);
    window.removeEventListener("popstate", cb);
  };
}

// Normalize fetchClusters() — it may return an array or {clusters:[]}.
export function normalizeClusters(r: unknown): ClusterLite[] {
  if (Array.isArray(r)) return r as ClusterLite[];
  const obj = r as { clusters?: ClusterLite[] } | null;
  return obj?.clusters ?? [];
}
