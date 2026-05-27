"use client";

import { useEffect, useMemo, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from "recharts";
import { fetchClusters, fetchBatchTimeseries } from "@/lib/api-client";
import { PageHeader, PageBody } from "@/components/design-system/page-shell";
import { Expandable } from "@/components/design-system/expandable";
import { engineBadge, isPostgres } from "@/lib/engine";

interface ClusterRow {
  cluster_id: string;
  engine?: string;
}

type Mode = "cluster" | "period";

interface MetricSpec {
  id: string;
  label: string;
  unit?: string;
  pgOnly?: boolean;
  fmt?: (v: number) => string;
}

// Subset of dashboard metrics — fits 2x3 grid cleanly and covers the signals
// a DBA usually compares (load, capacity, throughput).
const METRICS: MetricSpec[] = [
  { id: "cpu", label: "CPU %", unit: "%", fmt: (v) => v.toFixed(1) },
  { id: "aas", label: "Avg Active Sessions", fmt: (v) => v.toFixed(2) },
  { id: "connections", label: "Connections", fmt: (v) => v.toFixed(0) },
  { id: "read_iops", label: "Read IOPS", fmt: (v) => v.toFixed(0) },
  { id: "write_iops", label: "Write IOPS", fmt: (v) => v.toFixed(0) },
  {
    id: "replica_lag_ms",
    label: "Replica Lag",
    unit: "ms",
    fmt: (v) => v.toFixed(0),
  },
  {
    id: "xact_commit",
    label: "Tx / sec (PG)",
    pgOnly: true,
    fmt: (v) => v.toFixed(1),
  },
  {
    id: "tup_returned",
    label: "Tuples / sec (PG)",
    pgOnly: true,
    fmt: (v) => v.toFixed(0),
  },
];

const RANGE_OPTIONS = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 168 },
];

// Period offset preset for period-vs-period mode. The "B" series is the same
// window shifted back by (hours) so the two series align on relative time.
const PERIOD_SHIFT_LABEL: Record<number, string> = {
  1: "previous 1h",
  6: "previous 6h",
  24: "yesterday",
  168: "last week",
};

interface SeriesPoint {
  ts: string;
  value: number | string;
}

function n(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

// Merge two time series into a single chart-friendly array. The "B" series is
// reindexed so points align with "A" by relative position (0, 5min, 10min, …)
// — only practical way to overlay last-week vs this-week, since absolute ts
// values differ by 7 days.
function mergeForChart(
  a: SeriesPoint[],
  b: SeriesPoint[],
  labelA: string,
  labelB: string,
): Array<Record<string, string | number>> {
  const len = Math.max(a.length, b.length);
  const out: Array<Record<string, string | number>> = [];
  for (let i = 0; i < len; i++) {
    const ap = a[i];
    const bp = b[i];
    const row: Record<string, string | number> = {
      // x axis uses the A timestamp so the most recent window controls the
      // time labels. If A is empty, fall back to B.
      ts: ap ? formatTs(ap.ts) : bp ? formatTs(bp.ts) : "",
    };
    if (ap) row[labelA] = n(ap.value);
    if (bp) row[labelB] = n(bp.value);
    out.push(row);
  }
  return out;
}

function formatTs(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

export default function ComparePage() {
  const [clusters, setClusters] = useState<ClusterRow[]>([]);
  const [mode, setMode] = useState<Mode>("cluster");
  const [hours, setHours] = useState(24);

  // Cluster-vs-cluster
  const [clusterA, setClusterA] = useState<string>("");
  const [clusterB, setClusterB] = useState<string>("");

  // Period-vs-period
  const [periodCluster, setPeriodCluster] = useState<string>("");

  const [loadingA, setLoadingA] = useState(false);
  const [loadingB, setLoadingB] = useState(false);
  const [seriesA, setSeriesA] = useState<Record<string, SeriesPoint[]>>({});
  const [seriesB, setSeriesB] = useState<Record<string, SeriesPoint[]>>({});

  useEffect(() => {
    fetchClusters()
      .then((rows: ClusterRow[]) => {
        setClusters(rows);
        if (rows.length === 0) return;
        if (!clusterA) setClusterA(rows[0].cluster_id);
        if (!clusterB) setClusterB(rows[1]?.cluster_id || rows[0].cluster_id);
        if (!periodCluster) setPeriodCluster(rows[0].cluster_id);
      })
      .catch((e) => console.error("clusters fetch failed:", e));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Decide which engines we're comparing so we can hide PG-only metrics
  // when at least one side is MySQL.
  const engineA = clusters.find(
    (c) => c.cluster_id === (mode === "cluster" ? clusterA : periodCluster),
  )?.engine;
  const engineB = clusters.find((c) => c.cluster_id === clusterB)?.engine;
  const showPgOnly =
    mode === "cluster"
      ? isPostgres(engineA) && isPostgres(engineB)
      : isPostgres(engineA);

  const visibleMetrics = useMemo(
    () => METRICS.filter((m) => !m.pgOnly || showPgOnly),
    [showPgOnly],
  );

  const metricIds = visibleMetrics.map((m) => m.id);
  const metricsKey = metricIds.join(",");

  // Cluster mode — fetch both clusters in parallel.
  useEffect(() => {
    if (mode !== "cluster" || !clusterA || !clusterB || metricIds.length === 0)
      return;
    let cancelled = false;
    setLoadingA(true);
    setLoadingB(true);
    Promise.allSettled([
      fetchBatchTimeseries(clusterA, metricIds, hours, 0),
      fetchBatchTimeseries(clusterB, metricIds, hours, 0),
    ]).then(([a, b]) => {
      if (cancelled) return;
      if (a.status === "fulfilled") setSeriesA(a.value.series || {});
      if (b.status === "fulfilled") setSeriesB(b.value.series || {});
      setLoadingA(false);
      setLoadingB(false);
    });
    return () => {
      cancelled = true;
    };
  }, [mode, clusterA, clusterB, hours, metricsKey]);

  // Period mode — fetch same cluster twice with different offsets.
  useEffect(() => {
    if (mode !== "period" || !periodCluster || metricIds.length === 0) return;
    let cancelled = false;
    setLoadingA(true);
    setLoadingB(true);
    Promise.allSettled([
      fetchBatchTimeseries(periodCluster, metricIds, hours, 0),
      fetchBatchTimeseries(periodCluster, metricIds, hours * 2, hours),
    ]).then(([cur, prev]) => {
      if (cancelled) return;
      if (cur.status === "fulfilled") setSeriesA(cur.value.series || {});
      if (prev.status === "fulfilled") setSeriesB(prev.value.series || {});
      setLoadingA(false);
      setLoadingB(false);
    });
    return () => {
      cancelled = true;
    };
  }, [mode, periodCluster, hours, metricsKey]);

  const labelA = mode === "cluster" ? clusterA || "A" : "current";
  const labelB =
    mode === "cluster"
      ? clusterB || "B"
      : PERIOD_SHIFT_LABEL[hours] || `−${hours}h`;

  const colorA = "#fbbf24"; // amber
  const colorB = "#38bdf8"; // sky

  return (
    <PageBody>
      <PageHeader
        eyebrow="모니터"
        title="Compare"
        description="멀티 클러스터 비교 또는 같은 클러스터의 시간대별 변화를 사이드바이사이드로 확인."
        actions={
          <div className="flex items-center gap-1">
            {RANGE_OPTIONS.map((r) => (
              <button
                key={r.hours}
                onClick={() => setHours(r.hours)}
                className={`text-xs px-3 py-1.5 border transition-colors ${
                  hours === r.hours
                    ? "border-amber-500/60 text-amber-300 bg-amber-500/10"
                    : "border-zinc-700 text-zinc-400 hover:text-zinc-100"
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        }
      />

      <div className="flex items-center gap-2 mb-4">
        <button
          onClick={() => setMode("cluster")}
          className={`text-xs px-3 py-1.5 border transition-colors ${
            mode === "cluster"
              ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
              : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Cluster vs Cluster
        </button>
        <button
          onClick={() => setMode("period")}
          className={`text-xs px-3 py-1.5 border transition-colors ${
            mode === "period"
              ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
              : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Period vs Period
        </button>
      </div>

      {mode === "cluster" ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-6">
          <ClusterPicker
            label="A"
            color={colorA}
            value={clusterA}
            clusters={clusters}
            onChange={setClusterA}
          />
          <ClusterPicker
            label="B"
            color={colorB}
            value={clusterB}
            clusters={clusters}
            onChange={setClusterB}
          />
        </div>
      ) : (
        <div className="mb-6 flex flex-col md:flex-row md:items-center gap-3">
          <ClusterPicker
            label="cluster"
            color={colorA}
            value={periodCluster}
            clusters={clusters}
            onChange={setPeriodCluster}
          />
          <div className="text-xs text-zinc-500 leading-tight">
            <span className="text-amber-300">current</span> = last {hours}h ·{" "}
            <span className="text-sky-300">
              {PERIOD_SHIFT_LABEL[hours] || `previous ${hours}h`}
            </span>{" "}
            = same length, shifted back
          </div>
        </div>
      )}

      {clusters.length < 2 && mode === "cluster" ? (
        <div className="border border-amber-500/30 bg-amber-500/5 text-amber-300 text-sm px-4 py-3">
          Cluster vs Cluster needs at least 2 registered clusters. Add another
          via{" "}
          <a href="/clusters" className="underline">
            Clusters
          </a>{" "}
          or generate a sample cluster.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {visibleMetrics.map((m) => {
            const a = seriesA[m.id] || [];
            const b = seriesB[m.id] || [];
            const data = mergeForChart(a, b, labelA, labelB);
            const loading = loadingA || loadingB;
            return (
              <Expandable key={m.id} title={m.label}>
                <div className="bg-zinc-900/50 border border-zinc-800 p-3">
                  <div className="flex items-baseline justify-between mb-2 pr-8">
                    <div className="text-xs text-zinc-300">{m.label}</div>
                    {m.unit && (
                      <div className="text-[10px] text-zinc-500">{m.unit}</div>
                    )}
                  </div>
                  <div className="h-40">
                    {loading ? (
                      <div className="h-full flex items-center justify-center text-xs text-zinc-500">
                        loading…
                      </div>
                    ) : data.length === 0 ? (
                      <div className="h-full flex items-center justify-center text-xs text-zinc-600">
                        no data
                      </div>
                    ) : (
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart
                          data={data}
                          margin={{ top: 4, right: 8, bottom: 0, left: -20 }}
                        >
                          <CartesianGrid
                            strokeDasharray="3 3"
                            stroke="#27272a"
                            vertical={false}
                          />
                          <XAxis
                            dataKey="ts"
                            stroke="#71717a"
                            fontSize={9}
                            interval="preserveStartEnd"
                          />
                          <YAxis
                            stroke="#71717a"
                            fontSize={9}
                            tickFormatter={(v) =>
                              m.fmt ? m.fmt(Number(v)) : String(v)
                            }
                          />
                          <Tooltip
                            contentStyle={{
                              background: "#18181b",
                              border: "1px solid #3f3f46",
                              fontSize: 11,
                            }}
                            labelStyle={{ color: "#a1a1aa" }}
                            formatter={(value: unknown) => {
                              const num = Number(value);
                              if (!Number.isFinite(num))
                                return String(value ?? "—");
                              return m.fmt ? m.fmt(num) : String(num);
                            }}
                          />
                          <Legend wrapperStyle={{ fontSize: 10 }} />
                          <Line
                            type="monotone"
                            dataKey={labelA}
                            stroke={colorA}
                            strokeWidth={2}
                            dot={false}
                            isAnimationActive={false}
                          />
                          <Line
                            type="monotone"
                            dataKey={labelB}
                            stroke={colorB}
                            strokeWidth={2}
                            dot={false}
                            strokeDasharray={
                              mode === "period" ? "4 3" : undefined
                            }
                            isAnimationActive={false}
                          />
                        </LineChart>
                      </ResponsiveContainer>
                    )}
                  </div>
                </div>
              </Expandable>
            );
          })}
        </div>
      )}
    </PageBody>
  );
}

function ClusterPicker({
  label,
  color,
  value,
  clusters,
  onChange,
}: {
  label: string;
  color: string;
  value: string;
  clusters: ClusterRow[];
  onChange: (v: string) => void;
}) {
  const selected = clusters.find((c) => c.cluster_id === value);
  const badge = engineBadge(selected?.engine);
  return (
    <div className="flex items-center gap-2 bg-zinc-900/40 border border-zinc-800 px-3 py-2">
      <span className="w-2 h-2 rounded-full" style={{ background: color }} />
      <div className="font-mono text-[10px] tracking-wider uppercase text-zinc-500 w-16">
        {label}
      </div>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="flex-1 bg-zinc-950 text-zinc-100 border border-zinc-800 px-2 py-1 text-xs focus:outline-none focus:border-amber-500/60"
      >
        {clusters.length === 0 && <option value="">(no clusters)</option>}
        {clusters.map((c) => (
          <option key={c.cluster_id} value={c.cluster_id}>
            {c.cluster_id}
          </option>
        ))}
      </select>
      {selected && (
        <span
          className={`px-1.5 py-0.5 border text-[9px] font-mono uppercase tracking-wider ${badge.classes}`}
        >
          {badge.short}
        </span>
      )}
    </div>
  );
}
