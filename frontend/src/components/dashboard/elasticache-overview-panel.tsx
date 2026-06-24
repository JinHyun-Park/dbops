"use client";

import { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from "recharts";
import {
  fetchResourceDetails,
  fetchBatchTimeseries,
  type TimeRange,
} from "@/lib/api-client";
import { Expandable } from "@/components/design-system/expandable";
import { fmtDecimal, fmtExact } from "@/lib/format";
import { useChartColors } from "@/lib/use-chart-colors";

type Point = { ts: string; value: number | string };

// ElastiCache resource_details shape
interface ElastiCacheDetails {
  engine?: string | null; // "redis" | "memcached"
  engine_version?: string | null;
  node_type?: string | null;
  num_nodes?: number | null;
  num_shards?: number | null;
  multi_az?: boolean | null;
  status?: string | null;
  replication_group_id?: string | null;
}

function fmtTime(iso: string) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

// ─── Stat tile ───────────────────────────────────────────────────────────────

function StatTile({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="bg-zinc-950 border border-zinc-800 rounded p-3">
      <div className="text-[11px] text-zinc-500 mb-1">{label}</div>
      <div className="text-sm font-mono text-zinc-100">{value}</div>
      {sub && <div className="text-[10px] text-zinc-500 mt-0.5">{sub}</div>}
    </div>
  );
}

// ─── Mini timeseries panel ────────────────────────────────────────────────────

function MiniChart({
  title,
  series,
  loading,
  colors,
  unit,
  type = "line",
}: {
  title: string;
  series: { name: string; color: string; points: Point[] }[];
  loading: boolean;
  colors: ReturnType<typeof useChartColors>;
  unit?: string;
  type?: "line" | "area";
}) {
  const timeMap = new Map<string, Record<string, number>>();
  for (const s of series) {
    for (const p of s.points) {
      const t = fmtTime(p.ts);
      if (!timeMap.has(t)) timeMap.set(t, {});
      timeMap.get(t)![s.name] = Number(p.value) || 0;
    }
  }
  const data = Array.from(timeMap.entries()).map(([t, vals]) => ({
    ts: t,
    ...vals,
  }));

  const primaryPoints = series[0]?.points ?? [];
  const primaryCurrent = primaryPoints.length
    ? Number(primaryPoints[primaryPoints.length - 1].value) || 0
    : 0;

  return (
    <Expandable title={title}>
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        <div className="flex items-baseline justify-between mb-3 pr-6">
          <div className="text-sm text-zinc-200 font-medium">{title}</div>
          {unit && (
            <div className="text-[10px] text-zinc-500 uppercase tracking-wider">
              {unit}
            </div>
          )}
        </div>
        <div className="text-2xl font-semibold text-zinc-100 mb-3">
          {fmtDecimal(primaryCurrent, 2)}
          {unit && <span className="text-sm text-zinc-500 ml-1">{unit}</span>}
        </div>
        <div className="h-32">
          {loading ? (
            <div className="text-xs text-zinc-500 flex items-center h-full">
              불러오는 중…
            </div>
          ) : data.length === 0 ? (
            <div className="text-xs text-zinc-500 flex items-center h-full">
              데이터 없음
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              {type === "area" ? (
                <AreaChart
                  data={data}
                  margin={{ top: 4, right: 4, bottom: 0, left: -20 }}
                >
                  <CartesianGrid
                    strokeDasharray="3 3"
                    stroke={colors.grid}
                    vertical={false}
                  />
                  <XAxis dataKey="ts" stroke={colors.axis} fontSize={10} />
                  <YAxis stroke={colors.axis} fontSize={10} />
                  <Tooltip
                    contentStyle={{
                      background: colors.tooltipBg,
                      border: `1px solid ${colors.tooltipBorder}`,
                      fontSize: 12,
                    }}
                    labelStyle={{ color: colors.tooltipText }}
                  />
                  {series.length > 1 && (
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                  )}
                  {series.map((s) => (
                    <Area
                      key={s.name}
                      type="monotone"
                      dataKey={s.name}
                      stroke={s.color}
                      fill={s.color}
                      fillOpacity={0.15}
                      dot={false}
                    />
                  ))}
                </AreaChart>
              ) : (
                <LineChart
                  data={data}
                  margin={{ top: 4, right: 4, bottom: 0, left: -20 }}
                >
                  <CartesianGrid
                    strokeDasharray="3 3"
                    stroke={colors.grid}
                    vertical={false}
                  />
                  <XAxis dataKey="ts" stroke={colors.axis} fontSize={10} />
                  <YAxis stroke={colors.axis} fontSize={10} />
                  <Tooltip
                    contentStyle={{
                      background: colors.tooltipBg,
                      border: `1px solid ${colors.tooltipBorder}`,
                      fontSize: 12,
                    }}
                    labelStyle={{ color: colors.tooltipText }}
                  />
                  {series.length > 1 && (
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                  )}
                  {series.map((s) => (
                    <Line
                      key={s.name}
                      type="monotone"
                      dataKey={s.name}
                      stroke={s.color}
                      strokeWidth={2}
                      dot={false}
                    />
                  ))}
                </LineChart>
              )}
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </Expandable>
  );
}

// ─── Main panel ──────────────────────────────────────────────────────────────

const ELASTICACHE_METRICS = [
  "cache_cpu",
  "engine_cpu",
  "memory_usage_pct",
  "bytes_used",
  "cache_hits",
  "cache_misses",
  "get_hits",
  "get_misses",
  "curr_connections",
  "new_connections",
  "evictions",
  "replication_lag",
  "swap_usage",
  "freeable_memory",
  "curr_items",
  "net_in",
  "net_out",
] as const;

type ElastiCacheMetric = (typeof ELASTICACHE_METRICS)[number];

export function ElasticacheOverviewPanel({
  clusterId,
  range,
}: {
  clusterId: string;
  range: TimeRange;
}) {
  const chart = useChartColors();
  const [details, setDetails] = useState<ElastiCacheDetails | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(true);
  const [series, setSeries] = useState<Record<ElastiCacheMetric, Point[]>>(
    {} as Record<ElastiCacheMetric, Point[]>,
  );
  const [seriesLoading, setSeriesLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => {
      if (cancelled) return;
      setDetailsLoading(true);
      fetchResourceDetails(clusterId)
        .then((d) => {
          if (cancelled) return;
          setDetails((d.resource_details as ElastiCacheDetails) ?? null);
          setDetailsLoading(false);
        })
        .catch(() => {
          if (!cancelled) setDetailsLoading(false);
        });
    });
    return () => {
      cancelled = true;
    };
  }, [clusterId]);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchBatchTimeseries(clusterId, [...ELASTICACHE_METRICS], range)
        .then((d) => {
          if (cancelled) return;
          setSeries((d.series || {}) as Record<ElastiCacheMetric, Point[]>);
          setSeriesLoading(false);
        })
        .catch(() => {
          if (!cancelled) setSeriesLoading(false);
        });
    };
    load();
    const iv = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId, range]);

  // Determine if this is a Memcached cluster (different hit-rate metric keys)
  const isMemcached = (details?.engine ?? "")
    .toLowerCase()
    .includes("memcached");

  // Hit rate: derived from cache_hits/(cache_hits+cache_misses) for Redis,
  // get_hits/(get_hits+get_misses) for Memcached.
  // Build a synthetic point array for the hit-rate chart.
  const hitRatePoints: Point[] = (() => {
    const hits = isMemcached ? series.get_hits ?? [] : series.cache_hits ?? [];
    const misses = isMemcached
      ? series.get_misses ?? []
      : series.cache_misses ?? [];
    // Build a map keyed by timestamp
    const missMap = new Map<string, number>();
    for (const p of misses) missMap.set(p.ts, Number(p.value) || 0);
    return hits.map((p) => {
      const h = Number(p.value) || 0;
      const m = missMap.get(p.ts) ?? 0;
      const total = h + m;
      return { ts: p.ts, value: total > 0 ? (h / total) * 100 : 0 };
    });
  })();

  // Replication lag: hide the card entirely for Memcached (no replication) or
  // when the series is empty (single-node Redis). isMemcached is known from
  // details which loads independently, so gating on it avoids a flash while
  // seriesLoading is true.
  const replicationLagPoints = series.replication_lag ?? [];
  const showReplicationLag =
    !isMemcached && (replicationLagPoints.length > 0 || seriesLoading);

  return (
    <div className="space-y-6">
      {/* ─ Resource details tiles ─ */}
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        <div className="text-sm text-zinc-200 font-medium mb-3">
          클러스터 개요
        </div>
        {detailsLoading ? (
          <div className="text-zinc-500 text-sm">불러오는 중…</div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
            <StatTile
              label="Engine"
              value={
                details?.engine
                  ? `${details.engine}${
                      details.engine_version ? ` ${details.engine_version}` : ""
                    }`
                  : "—"
              }
            />
            <StatTile label="Node Type" value={details?.node_type ?? "—"} />
            <StatTile
              label="노드 수"
              value={
                details?.num_nodes != null ? fmtExact(details.num_nodes) : "—"
              }
              sub={
                details?.num_shards != null
                  ? `${fmtExact(details.num_shards)} shard(s)`
                  : undefined
              }
            />
            <StatTile label="Status" value={details?.status ?? "—"} />
            <StatTile
              label="Multi-AZ"
              value={
                details?.multi_az != null
                  ? details.multi_az
                    ? "enabled"
                    : "disabled"
                  : "—"
              }
            />
          </div>
        )}
      </div>

      {/* ─ Memory + Hit Rate ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          메모리 / Hit Rate
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <MiniChart
            title="Memory Usage"
            series={[
              {
                name: "memory_usage_pct",
                color: "#f472b6",
                points: series.memory_usage_pct ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="%"
            type="area"
          />
          <MiniChart
            title="Hit Rate"
            series={[
              {
                name: "hit_rate",
                color: "#34d399",
                points: hitRatePoints,
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="%"
            type="area"
          />
          <MiniChart
            title="Bytes Used"
            series={[
              {
                name: "bytes_used",
                color: "#a78bfa",
                points: (series.bytes_used ?? []).map((p) => ({
                  ...p,
                  value: Number(p.value) / 1073741824,
                })),
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="GB"
            type="area"
          />
          <MiniChart
            title="Freeable Memory"
            series={[
              {
                name: "freeable_memory",
                color: "#34d399",
                points: (series.freeable_memory ?? []).map((p) => ({
                  ...p,
                  value: Number(p.value) / 1073741824,
                })),
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="GB"
            type="area"
          />
        </div>
      </div>

      {/* ─ Evictions + Connections ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          Evictions / Connections
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <MiniChart
            title="Evictions"
            series={[
              {
                name: "evictions",
                color: "#fb7185",
                points: series.evictions ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
          <MiniChart
            title="Connections"
            series={[
              {
                name: "curr_connections",
                color: "#60a5fa",
                points: series.curr_connections ?? [],
              },
              {
                name: "new_connections",
                color: "#94a3b8",
                points: series.new_connections ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            type="area"
          />
        </div>
      </div>

      {/* ─ CPU ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          CPU
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <MiniChart
            title="CPU Utilization"
            series={[
              {
                name: "cache_cpu",
                color: "#fbbf24",
                points: series.cache_cpu ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="%"
            type="area"
          />
          <MiniChart
            title="Engine CPU Utilization"
            series={[
              {
                name: "engine_cpu",
                color: "#fb923c",
                points: series.engine_cpu ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="%"
            type="area"
          />
        </div>
      </div>

      {/* ─ Replication lag — hidden for Memcached / single-node ─ */}
      {showReplicationLag && (
        <div>
          <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
            Replication
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <MiniChart
              title="Replication Lag"
              series={[
                {
                  name: "replication_lag",
                  color: "#ef4444",
                  points: replicationLagPoints,
                },
              ]}
              loading={seriesLoading}
              colors={chart}
              unit="s"
            />
          </div>
        </div>
      )}

      {/* ─ Network throughput ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          네트워크 처리량 (Network Throughput)
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <MiniChart
            title="Network Bytes In"
            series={[
              {
                name: "net_in",
                color: "#22d3ee",
                points: (series.net_in ?? []).map((p) => ({
                  ...p,
                  value: Number(p.value) / 1073741824,
                })),
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="GB"
            type="area"
          />
          <MiniChart
            title="Network Bytes Out"
            series={[
              {
                name: "net_out",
                color: "#a78bfa",
                points: (series.net_out ?? []).map((p) => ({
                  ...p,
                  value: Number(p.value) / 1073741824,
                })),
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="GB"
            type="area"
          />
        </div>
      </div>

      {/* ─ Items + Swap ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          Items / Swap
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <MiniChart
            title="Current Items"
            series={[
              {
                name: "curr_items",
                color: "#60a5fa",
                points: series.curr_items ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
          <MiniChart
            title="Swap Usage"
            series={[
              {
                name: "swap_usage",
                color: "#fb7185",
                points: (series.swap_usage ?? []).map((p) => ({
                  ...p,
                  value: Number(p.value) / 1073741824,
                })),
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="GB"
            type="area"
          />
        </div>
      </div>
    </div>
  );
}
