"use client";

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchAnomalies } from "@/lib/api-client";
import { streamChat } from "@/lib/agentcore-sse";
import { fmtDecimal } from "@/lib/format";

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
  cpu: "CPU 사용률",
  cpu_util: "CPU 사용률",
  cpu_utilization: "CPU 사용률",
  aas: "활성 세션 (AAS)",
  connections: "활성 커넥션",
  conn: "활성 커넥션",
  deadlocks: "데드락",
  blocking_locks: "블로킹 락",
  storage_size_gb: "스토리지 사용량",
  buffer_cache_hit_ratio: "버퍼 캐시 적중률",
  replication_lag_ms: "복제 지연",
};

function prettyMetric(m: string): string {
  if (!m) return "지표";
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
        <div className="text-sm text-zinc-200 font-medium">
          Anomalies
          {items.length > 0 && (
            <span className="ml-2 px-1.5 py-0.5 bg-rose-500/20 text-rose-300 rounded text-[10px]">
              {items.length}
            </span>
          )}
        </div>
        <div className="text-[10px] text-zinc-500">
          z-score ≥ 2.5 (7일 베이스라인 대비)
        </div>
      </div>
      {loading ? (
        <div className="text-zinc-500 text-sm">불러오는 중…</div>
      ) : items.length === 0 ? (
        <div className="text-emerald-400 text-sm flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-500" />
          최근 4시간 동안 이상 징후 없음
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((a) => {
            const z = Math.abs(n(a.z_score));
            const styles =
              z > 5
                ? {
                    box: "border-rose-500/30 bg-rose-500/5 hover:bg-rose-500/10",
                    text: "text-rose-400",
                  }
                : z > 3
                  ? {
                      box: "border-amber-500/30 bg-amber-500/5 hover:bg-amber-500/10",
                      text: "text-amber-400",
                    }
                  : {
                      box: "border-sky-500/30 bg-sky-500/5 hover:bg-sky-500/10",
                      text: "text-sky-400",
                    };
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
                        title="요일·시간대별 과거 분포(중앙값 + IQR)와 비교"
                      >
                        seasonal
                      </span>
                    ) : a.mode === "flat" ? (
                      <span
                        className="text-[9px] uppercase tracking-wider px-1 py-0.5 border border-zinc-700 text-zinc-500 rounded-sm"
                        title="해당 시간대의 seasonal 베이스라인이 아직 학습되지 않아 단순 7일 평균±표준편차로 대체"
                      >
                        flat
                      </span>
                    ) : null}
                  </div>
                  <div className="text-[11px] text-zinc-500">
                    베이스라인 {fmtDecimal(n(a.baseline_mean), 2)} · 최근 최댓값{" "}
                    <span className="text-zinc-300">
                      {fmtDecimal(n(a.recent_max), 2)}
                    </span>
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
      `Aurora 클러스터에서 이상 징후가 감지됐어. **한국어로** 다음 3개 섹션으로 짧고 명확하게 진단해줘:\n` +
      `1. **추정 원인** — 메트릭 종류와 패턴을 보고 가장 그럴듯한 설명 ` +
      `(애플리케이션 워크로드 급증, 폭주 쿼리, 배포, 플래너 회귀, 락 스톰 등).\n` +
      `2. **운영 영향** — 지금 사용자나 애플리케이션이 어떤 경험을 하고 있을지.\n` +
      `3. **다음 점검 단계** — 원인을 확정하기 위해 실행할 구체적인 쿼리 1건 또는 MCP 도구 1개. ` +
      `안전하다면 직접 실행해서 결과까지 포함해줘.\n\n` +
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

  const sevTone = z > 5 ? "rose" : z > 3 ? "amber" : "sky";
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
              <span
                className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${sevBadge}`}
              >
                이상 징후 · σ{z.toFixed(1)}
              </span>
              <span className="text-[10px] text-zinc-500 font-mono">
                {anomaly.metric_type}
              </span>
            </div>
            <h2 className="text-lg font-semibold text-zinc-100">
              {prettyLabel}
            </h2>
            <div className="text-xs text-zinc-400 mt-1">
              베이스라인 {fmtDecimal(baseline, 2)} ± {fmtDecimal(stddev, 2)} ·
              최근 최댓값{" "}
              <span className="text-zinc-200">{fmtDecimal(recentMax, 2)}</span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-200 text-xl leading-none ml-3"
            aria-label="닫기"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="grid grid-cols-2 gap-3 mb-4">
            <Stat
              label="최근 최댓값"
              value={fmtDecimal(recentMax, 2)}
              tone={sevTone}
            />
            <Stat label="최근 평균" value={fmtDecimal(recentAvg, 2)} />
            <Stat label="베이스라인 평균" value={fmtDecimal(baseline, 2)} />
            <Stat label="베이스라인 σ" value={fmtDecimal(stddev, 2)} />
          </div>

          <div className="border-t border-zinc-800 pt-3">
            <div className="flex items-center justify-between mb-2">
              <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
                AI 진단
              </div>
              <button
                onClick={handleAnalyze}
                disabled={insightLoading}
                className="text-xs px-3 py-1 border border-sky-500/40 text-sky-300 hover:bg-sky-500/10 disabled:opacity-50 transition-colors"
              >
                {insightLoading
                  ? "분석 중…"
                  : insight
                    ? "다시 진단"
                    : "원인 진단 + 다음 점검"}
              </button>
            </div>
            {insightError && (
              <div className="text-xs text-rose-400 border border-rose-500/40 bg-rose-500/10 px-3 py-2 mb-2">
                {insightError}
              </div>
            )}
            {!insight && !insightLoading && !insightError && (
              <div className="text-xs text-zinc-500">
                <span className="text-sky-300">원인 진단 + 다음 점검</span>{" "}
                버튼을 누르면 추정 원인, 운영 영향, 다음 점검 단계를 한 번에
                받아볼 수 있어요.
              </div>
            )}
            {insight && (
              <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-zinc-950 prose-pre:border prose-pre:border-zinc-800 prose-code:text-sky-300">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {insight}
                </ReactMarkdown>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "rose" | "amber" | "sky";
}) {
  const color =
    tone === "rose"
      ? "text-rose-300"
      : tone === "amber"
        ? "text-amber-300"
        : "text-zinc-100";
  return (
    <div className="border border-zinc-800 bg-zinc-900/40 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className={`text-base mt-0.5 tabular-nums ${color}`}>{value}</div>
    </div>
  );
}
