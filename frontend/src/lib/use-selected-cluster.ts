"use client";

// Page-level hook over the shared selected-cluster store. A cluster-scoped page
// uses this instead of its own fetchClusters + useState(<select>) so that:
//   - the active cluster is the GLOBAL one (URL ?cluster= ?? localStorage),
//     validated against the real cluster list (else the first cluster),
//   - switching anywhere (⌘K palette, header chip, another page) LIVE-updates
//     this page via the change event — selection persists across navigation,
//   - setSelected writes back to the shared store so everyone else follows.
import { useCallback, useEffect, useState } from "react";
import { fetchClusters } from "@/lib/api-client";
import {
  getSelectedCluster,
  normalizeClusters,
  onClusterChange,
  setSelectedCluster as persist,
  type ClusterLite,
} from "@/lib/selected-cluster";

export function useSelectedCluster(): {
  clusters: ClusterLite[];
  selected: string | null;
  setSelected: (id: string) => void;
  loading: boolean;
} {
  const [clusters, setClusters] = useState<ClusterLite[]>([]);
  const [selected, setSel] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchClusters()
      .then((r: unknown) => {
        if (cancelled) return;
        const cs = normalizeClusters(r);
        setClusters(cs);
        const cur = getSelectedCluster();
        // Honor the global selection if it's a real cluster; else default to the
        // first one (and pin it into the store so the rest of the app agrees).
        const valid = cur && cs.some((c) => c.cluster_id === cur);
        const next = valid ? cur : cs[0]?.cluster_id ?? null;
        setSel(next);
        if (next && next !== cur) persist(next);
      })
      .catch(() => {})
      .finally(() => !cancelled && setLoading(false));

    // Live-sync when the cluster is switched elsewhere (palette / header / back).
    const unsub = onClusterChange(() => {
      const cur = getSelectedCluster();
      if (cur) setSel(cur);
    });
    return () => {
      cancelled = true;
      unsub();
    };
  }, []);

  const setSelected = useCallback((id: string) => {
    setSel(id);
    persist(id);
  }, []);

  return { clusters, selected, setSelected, loading };
}
