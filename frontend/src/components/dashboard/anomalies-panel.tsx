"use client";

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchAnomalies } from "@/lib/api-client";
import { streamChat } from "@/lib/agentcore-sse";

interface Anomaly {
  metric_type: string;
  recent_max: number | string;
  recent_avg: number | string;
  baseline_mean: number | string;
  baseline_stddev: number | string;
  z_score: number | string;
  mode?: "seasonal" | "flat";
  sample_count?: number | null;
}

function n(v: unknown): number {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

const METRIC_LABELS: Record<string, string> = {
  cpu: "CPU utilization",
  cpu_util: "CPU utilization",
  cpu_utilization: "CPU utilization",
  aas: "Active sessions (AAS)",
  connections: "Active connections",
  conn: "Active connections",
  deadlocks: "Deadlocks",
  blocking_locks: "Blocking locks",
  storage_size_gb: "Storage size",
  buffer_cache_hit_ratio: "Buffer cache hit ratio",
  replication_lag_ms: "Replication lag",
};

function prettyMetric(m: string): string {
  if (!m) return "metric";
  return METRIC_LABELS[m.toLowerCase()] || m;
}

export function AnomaliesPanel({ clusterId }: { clusterId: string }) {
  const [items, setItems] = useState<Anomaly[]>([]);
  const [loading, setLoading] = useState(true);
  const [active, setActive] = useState<Anomaly | null>(null);

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
                ? { box: "border-rose-500/30 bg-rose-500/5 hover:bg-rose-500/10", text: "text-rose-400" }
                : z > 3
                ? { box: "border-amber-500/30 bg-amber-500/5 hover:bg-amber-500/10", text: "text-amber-400" }
                : { box: "border-sky-500/30 bg-sky-500/5 hover:bg-sky-500/10", text: "text-sky-400" };
            return (
              <button
                key={a.metric_type}
                onClick={() => setActive(a)}
                className={`w-full text-left flex items-center justify-between p-2 rounded border transition-colors ${styles.box}`}
              >
                <div>
                  <div className="text-sm text-zinc-200 flex items-center gap-1.5">
                    {prettyMetric(a.metric_type)}
                    {a.mode === "seasonal" ? (
                      <span
                        className="text-[9px] uppercase tracking-wider px-1 py-0.5 border border-emerald-500/40 text-emerald-300 rounded-sm"
                        title="Compared against this hour-of-week's historical bucket (median + IQR)"
                      >
                        seasonal
                      </span>
                    ) : a.mode === "flat" ? (
                      <span
                        className="text-[9px] uppercase tracking-wider px-1 py-0.5 border border-zinc-700 text-zinc-500 rounded-sm"
                        title="Falling back to flat 7-day mean+stddev — seasonal baseline not yet trained for this bucket"
                      >
                        flat
                      </span>
                    ) : null}
                  </div>
                  <div className="text-[11px] text-zinc-500">
                    baseline {n(a.baseline_mean).toFixed(2)} · current max{" "}
                    <span className="text-zinc-300">{n(a.recent_max).toFixed(2)}</span>
                  </div>
                </div>
                <div className={`text-sm font-mono font-medium ${styles.text}`}>
                  σ{z.toFixed(1)}
                </div>
              </button>
            );
          })}
        </div>
      )}
      {active && (
        <AnomalyDetailModal
          anomaly={active}
          clusterId={clusterId}
          prettyLabel={prettyMetric(active.metric_type)}
          onClose={() => setActive(null)}
        />
      )}
    </div>
  );
}

function AnomalyDetailModal({
  anomaly,
  clusterId,
  prettyLabel,
  onClose,
}: {
  anomaly: Anomaly;
  clusterId: string;
  prettyLabel: string;
  onClose: () => void;
}) {
  const [insight, setInsight] = useState("");
  const [insightLoading, setInsightLoading] = useState(false);
  const [insightError, setInsightError] = useState<string | null>(null);

  const z = Math.abs(n(anomaly.z_score));
  const baseline = n(anomaly.baseline_mean);
  const stddev = n(anomaly.baseline_stddev);
  const recentMax = n(anomaly.recent_max);
  const recentAvg = n(anomaly.recent_avg);

  const handleAnalyze = () => {
    setInsight("");
    setInsightError(null);
    setInsightLoading(true);
    const message =
      `An anomaly was detected on our Aurora cluster. Diagnose it in 3 short sections:\n` +
      `1. **Likely cause** — the most plausible explanation, given the metric and shape ` +
      `(application workload spike, runaway query, deploy, planner regression, lock storm, etc.).\n` +
      `2. **Operational impact** — what users / app would currently experience.\n` +
      `3. **What to investigate next** — one concrete query or MCP tool you'd run to confirm. ` +
      `If safe, also run that tool yourself and include the finding.\n\n` +
      `Cluster: ${clusterId}\n` +
      `Metric: ${anomaly.metric_type} (${prettyLabel})\n` +
      `Recent window max: ${recentMax}\n` +
      `Recent window avg: ${recentAvg}\n` +
      `7-day baseline mean: ${baseline}\n` +
      `7-day baseline stddev: ${stddev}\n` +
      `Z-score: ${z.toFixed(2)} (threshold 2.5)\n`;

    streamChat(
      message,
      clusterId,
      (tok) => setInsight((prev) => prev + tok),
      () => {},
      () => setInsightLoading(false),
      (err) => {
        setInsightError(err.message);
        setInsightLoading(false);
      },
    );
  };

  const sevTone =
    z > 5 ? "rose" : z > 3 ? "amber" : "sky";
  const sevBadge =
    sevTone === "rose"
      ? "bg-rose-500/20 text-rose-300 border border-rose-500/40"
      : sevTone === "amber"
      ? "bg-amber-500/15 text-amber-300 border border-amber-500/40"
      : "bg-sky-500/15 text-sky-300 border border-sky-500/30";

  return (
    <div
      className="fixed inset-0 z-50 bg-zinc-950/80 backdrop-blur flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl max-h-[90vh] flex flex-col bg-zinc-900 border border-zinc-700 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between px-5 py-4 border-b border-zinc-800">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <span className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${sevBadge}`}>
                anomaly · σ{z.toFixed(1)}
              </span>
              <span className="text-[10px] text-zinc-500 font-mono">{anomaly.metric_type}</span>
            </div>
            <h2 className="text-lg font-semibold text-zinc-100">{prettyLabel}</h2>
            <div className="text-xs text-zinc-400 mt-1">
              baseline {baseline.toFixed(2)} ± {stddev.toFixed(2)} · current max{" "}
              <span className="text-zinc-200">{recentMax.toFixed(2)}</span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-200 text-xl leading-none ml-3"
            aria-label="close"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="grid grid-cols-2 gap-3 mb-4">
            <Stat label="Recent max" value={recentMax.toFixed(2)} tone={sevTone} />
            <Stat label="Recent avg" value={recentAvg.toFixed(2)} />
            <Stat label="Baseline mean" value={baseline.toFixed(2)} />
            <Stat label="Baseline σ" value={stddev.toFixed(2)} />
          </div>

          <div className="border-t border-zinc-800 pt-3">
            <div className="flex items-center justify-between mb-2">
              <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
                AI diagnosis
              </div>
              <button
                onClick={handleAnalyze}
                disabled={insightLoading}
                className="text-xs px-3 py-1 border border-sky-500/40 text-sky-300 hover:bg-sky-500/10 disabled:opacity-50 transition-colors"
              >
                {insightLoading ? "thinking…" : insight ? "Re-analyze" : "Diagnose + suggest probe"}
              </button>
            </div>
            {insightError && (
              <div className="text-xs text-rose-400 border border-rose-500/40 bg-rose-500/10 px-3 py-2 mb-2">
                {insightError}
              </div>
            )}
            {!insight && !insightLoading && !insightError && (
              <div className="text-xs text-zinc-500">
                Click <span className="text-sky-300">Diagnose + suggest probe</span> for likely cause,
                impact, and a specific next check.
              </div>
            )}
            {insight && (
              <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-zinc-950 prose-pre:border prose-pre:border-zinc-800 prose-code:text-sky-300">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{insight}</ReactMarkdown>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "rose" | "amber" | "sky" }) {
  const color =
    tone === "rose"
      ? "text-rose-300"
      : tone === "amber"
      ? "text-amber-300"
      : "text-zinc-100";
  return (
    <div className="border border-zinc-800 bg-zinc-900/40 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className={`text-base mt-0.5 tabular-nums ${color}`}>{value}</div>
    </div>
  );
}
