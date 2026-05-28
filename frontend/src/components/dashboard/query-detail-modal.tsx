"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { fetchQueryDetail } from "@/lib/api-client";
import { fmtDuration, fmtExact, fmtNumber } from "@/lib/format";
import { useChartColors } from "@/lib/use-chart-colors";

interface Snapshot {
  snapshot_time: string;
  calls: number | string;
  total_time_ms: number | string;
  mean_time_ms: number | string;
  rows_returned: number | string;
  shared_blks_hit: number | string;
  shared_blks_read: number | string;
  query_text: string;
}

function n(v: unknown): number {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

export function QueryDetailModal({
  clusterId,
  queryHash,
  onClose,
}: {
  clusterId: string;
  queryHash: string;
  onClose: () => void;
}) {
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const chart = useChartColors();

  useEffect(() => {
    fetchQueryDetail(clusterId, queryHash)
      .then((d) => {
        setSnapshots(d.snapshots || []);
        setLoading(false);
      })
      .catch((e) => {
        setErr(e.message);
        setLoading(false);
      });
  }, [clusterId, queryHash]);

  const latest = snapshots[0];
  const chartData = [...snapshots].reverse().map((s) => ({
    ts: new Date(s.snapshot_time).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    }),
    mean: n(s.mean_time_ms),
    calls: n(s.calls),
  }));

  const cacheHitRatio = latest
    ? (n(latest.shared_blks_hit) /
        Math.max(1, n(latest.shared_blks_hit) + n(latest.shared_blks_read))) *
      100
    : 0;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-5"
      onClick={onClose}
    >
      <div
        className="bg-zinc-900 border border-zinc-800 max-w-4xl w-full max-h-[90vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-center px-6 py-4 border-b border-zinc-800">
          <div>
            <div className="text-sm text-zinc-200 font-medium">
              Query Detail
            </div>
            <div className="text-sm text-zinc-300 font-mono">
              {queryHash.slice(0, 16)}…
            </div>
          </div>
          <div className="flex items-center gap-3">
            {latest && (
              <Link
                href={`/chat?prompt=${encodeURIComponent(
                  `클러스터 ${clusterId}에서 다음 쿼리에 대해 EXPLAIN (FORMAT JSON)을 실행하고, **한국어로** 실행 계획을 요약 + 가장 비싼 노드 강조 + 개선안을 제안해줘:\n\n${latest.query_text}`,
                )}`}
                className="text-xs bg-sky-600 hover:bg-sky-500 text-white rounded px-3 py-1.5 transition"
              >
                Chat에서 분석
              </Link>
            )}
            <button
              onClick={onClose}
              className="text-zinc-400 hover:text-zinc-100 text-xl leading-none px-2"
            >
              ×
            </button>
          </div>
        </div>

        <div className="overflow-y-auto p-6 space-y-6">
          {loading ? (
            <div className="text-zinc-500 text-sm">불러오는 중…</div>
          ) : err ? (
            <div className="text-red-400 text-sm">{err}</div>
          ) : !latest ? (
            <div className="text-zinc-500 text-sm">스냅샷 없음</div>
          ) : (
            <>
              <div>
                <div className="text-sm text-zinc-200 font-medium mb-2">
                  SQL
                </div>
                <pre className="bg-zinc-950 border border-zinc-800 rounded p-3 text-xs text-zinc-200 font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
                  {latest.query_text}
                </pre>
              </div>

              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <Metric
                  label="Calls"
                  value={fmtNumber(n(latest.calls))}
                  title={`${fmtExact(n(latest.calls))} calls`}
                />
                <Metric
                  label="Mean / call"
                  value={fmtDuration(n(latest.mean_time_ms))}
                  title={`${n(latest.mean_time_ms).toFixed(2)} ms per call`}
                />
                <Metric
                  label="Total time"
                  value={fmtDuration(n(latest.total_time_ms))}
                  title={`${n(latest.total_time_ms).toFixed(2)} ms total`}
                />
                <Metric
                  label="Rows returned"
                  value={fmtNumber(n(latest.rows_returned))}
                  title={`${fmtExact(n(latest.rows_returned))} rows`}
                />
                <Metric
                  label="Buffer cache hit"
                  value={cacheHitRatio.toFixed(1) + "%"}
                  title="shared_blks_hit / (shared_blks_hit + shared_blks_read)"
                />
                <Metric
                  label="Blocks hit"
                  value={fmtNumber(n(latest.shared_blks_hit))}
                  title={`${fmtExact(
                    n(latest.shared_blks_hit),
                  )} pages served from cache`}
                />
                <Metric
                  label="Blocks read"
                  value={fmtNumber(n(latest.shared_blks_read))}
                  title={`${fmtExact(
                    n(latest.shared_blks_read),
                  )} pages read from disk`}
                />
                <Metric
                  label="Snapshots"
                  value={fmtNumber(snapshots.length)}
                  title={`${snapshots.length} stat snapshots`}
                />
              </div>

              {chartData.length > 1 && (
                <div>
                  <div className="text-sm text-zinc-200 font-medium mb-2">
                    Mean Time Trend ({snapshots.length} snapshots)
                  </div>
                  <div className="h-48 bg-zinc-950 border border-zinc-800 rounded p-2">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart
                        data={chartData}
                        margin={{ top: 8, right: 8, bottom: 4, left: -20 }}
                      >
                        <CartesianGrid
                          strokeDasharray="3 3"
                          stroke={chart.grid}
                          vertical={false}
                        />
                        <XAxis dataKey="ts" stroke={chart.axis} fontSize={10} />
                        <YAxis stroke={chart.axis} fontSize={10} />
                        <Tooltip
                          contentStyle={{
                            background: chart.tooltipBg,
                            border: `1px solid ${chart.tooltipBorder}`,
                            fontSize: 12,
                          }}
                          labelStyle={{ color: chart.tooltipText }}
                        />
                        <Line
                          type="monotone"
                          dataKey="mean"
                          stroke={chart.amber}
                          strokeWidth={2}
                          dot={false}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  title,
}: {
  label: string;
  value: string;
  title?: string;
}) {
  return (
    <div
      className="bg-zinc-950 border border-zinc-800 rounded p-3"
      title={title}
    >
      <div className="text-xs text-zinc-500 mb-1">{label}</div>
      <div className="text-zinc-100 font-mono text-sm tabular-nums">
        {value}
      </div>
    </div>
  );
}
