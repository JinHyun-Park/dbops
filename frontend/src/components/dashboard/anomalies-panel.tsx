"use client";

import { useEffect, useState } from "react";
import { fetchAnomalies } from "@/lib/api-client";

interface Anomaly {
  metric_type: string;
  recent_max: number | string;
  recent_avg: number | string;
  baseline_mean: number | string;
  baseline_stddev: number | string;
  z_score: number | string;
}

function n(v: unknown): number {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

export function AnomaliesPanel({ clusterId }: { clusterId: string }) {
  const [items, setItems] = useState<Anomaly[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchAnomalies(clusterId, 4, 2.5)
        .then((d) => !cancelled && setItems(d.anomalies || []))
        .catch(() => !cancelled && setItems([]))
        .finally(() => !cancelled && setLoading(false));
    load();
    const iv = setInterval(load, 60000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId]);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="text-xs text-zinc-400 uppercase tracking-wider">
          Anomalies
          {items.length > 0 && (
            <span className="ml-2 px-1.5 py-0.5 bg-rose-500/20 text-rose-300 rounded text-[10px]">
              {items.length}
            </span>
          )}
        </div>
        <div className="text-[10px] text-zinc-500">z-score ≥ 2.5 vs 7d baseline</div>
      </div>
      {loading ? (
        <div className="text-zinc-500 text-sm">Loading...</div>
      ) : items.length === 0 ? (
        <div className="text-emerald-400 text-sm flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-500" />
          no anomalies in last 4 hours
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((a) => {
            const z = Math.abs(n(a.z_score));
            const styles =
              z > 5
                ? { box: "border-rose-500/30 bg-rose-500/5", text: "text-rose-400" }
                : z > 3
                ? { box: "border-amber-500/30 bg-amber-500/5", text: "text-amber-400" }
                : { box: "border-sky-500/30 bg-sky-500/5", text: "text-sky-400" };
            return (
              <div
                key={a.metric_type}
                className={`flex items-center justify-between p-2 rounded border ${styles.box}`}
              >
                <div>
                  <div className="text-sm text-zinc-200 font-mono">{a.metric_type}</div>
                  <div className="text-[11px] text-zinc-500">
                    baseline {n(a.baseline_mean).toFixed(2)} · current max{" "}
                    <span className="text-zinc-300">{n(a.recent_max).toFixed(2)}</span>
                  </div>
                </div>
                <div className={`text-sm font-mono font-medium ${styles.text}`}>
                  σ{z.toFixed(1)}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
