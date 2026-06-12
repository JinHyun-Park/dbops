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
import { fmtDecimal } from "@/lib/format";
import { useChartColors } from "@/lib/use-chart-colors";

type Point = { ts: string; value: number | string };

// DocumentDB-specific resource_details shape
interface DocDbDetails {
  instances?: Array<{
    instance_id: string;
    instance_class?: string;
    status?: string;
    [k: string]: unknown;
  }> | null;
  instance_count?: number | null;
  engine_version?: string | null;
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

const DOCDB_METRICS = [
  "db_connections",
  "replica_lag_ms",
  "buffer_cache_hit",
  "cursors",
  "cursors_timed_out",
  "opcounter_query",
  "opcounter_insert",
  "opcounter_update",
  "opcounter_delete",
  "cpu_utilization",
  "freeable_memory",
  "read_latency_ms",
  "write_latency_ms",
  "disk_queue_depth",
  "storage_bytes",
] as const;

type DocDbMetric = (typeof DOCDB_METRICS)[number];

export function DocdbOverviewPanel({
  clusterId,
  range,
}: {
  clusterId: string;
  range: TimeRange;
}) {
  const chart = useChartColors();
  const [details, setDetails] = useState<DocDbDetails | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(true);
  const [series, setSeries] = useState<Record<DocDbMetric, Point[]>>(
    {} as Record<DocDbMetric, Point[]>,
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
          setDetails((d.resource_details as DocDbDetails) ?? null);
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
      fetchBatchTimeseries(clusterId, [...DOCDB_METRICS], range)
        .then((d) => {
          if (cancelled) return;
          setSeries((d.series || {}) as Record<DocDbMetric, Point[]>);
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

  const instances = details?.instances ?? [];

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
          <>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-3">
              <StatTile
                label="인스턴스 수"
                value={String(
                  details?.instance_count ?? instances.length ?? "—",
                )}
              />
              <StatTile
                label="엔진 버전"
                value={details?.engine_version ?? "—"}
              />
            </div>
            {instances.length > 0 && (
              <div className="space-y-1">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  인스턴스 목록
                </div>
                {instances.map((inst) => (
                  <div
                    key={inst.instance_id}
                    className="flex items-center gap-3 text-xs font-mono bg-zinc-950 border border-zinc-800 rounded px-3 py-1.5"
                  >
                    <span className="text-zinc-300">{inst.instance_id}</span>
                    {inst.instance_class && (
                      <span className="text-zinc-500">
                        {inst.instance_class}
                      </span>
                    )}
                    {inst.status && (
                      <span
                        className={
                          inst.status === "available"
                            ? "text-emerald-400"
                            : "text-amber-400"
                        }
                      >
                        {inst.status}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {/* ─ Connections + Replica Lag ─ */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <MiniChart
          title="DB Connections"
          series={[
            {
              name: "db_connections",
              color: "#60a5fa",
              points: series.db_connections ?? [],
            },
          ]}
          loading={seriesLoading}
          colors={chart}
          type="area"
        />
        <MiniChart
          title="Replica Lag"
          series={[
            {
              name: "replica_lag_ms",
              color: "#fb7185",
              points: series.replica_lag_ms ?? [],
            },
          ]}
          loading={seriesLoading}
          colors={chart}
          unit="ms"
        />
      </div>

      {/* ─ Buffer Cache Hit + CPU ─ */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <MiniChart
          title="Buffer Cache Hit"
          series={[
            {
              name: "buffer_cache_hit",
              color: "#34d399",
              points: series.buffer_cache_hit ?? [],
            },
          ]}
          loading={seriesLoading}
          colors={chart}
          unit="%"
          type="area"
        />
        <MiniChart
          title="CPU Utilization"
          series={[
            {
              name: "cpu_utilization",
              color: "#fbbf24",
              points: series.cpu_utilization ?? [],
            },
          ]}
          loading={seriesLoading}
          colors={chart}
          unit="%"
          type="area"
        />
      </div>

      {/* ─ Cursors ─ */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <MiniChart
          title="Cursors"
          series={[
            {
              name: "cursors",
              color: "#a78bfa",
              points: series.cursors ?? [],
            },
          ]}
          loading={seriesLoading}
          colors={chart}
        />
        <MiniChart
          title="Cursors Timed Out"
          series={[
            {
              name: "cursors_timed_out",
              color: "#ef4444",
              points: series.cursors_timed_out ?? [],
            },
          ]}
          loading={seriesLoading}
          colors={chart}
        />
      </div>

      {/* ─ Opcounters ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          연산 카운터 (Opcounters)
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <MiniChart
            title="Query"
            series={[
              {
                name: "opcounter_query",
                color: "#22d3ee",
                points: series.opcounter_query ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
          <MiniChart
            title="Insert"
            series={[
              {
                name: "opcounter_insert",
                color: "#34d399",
                points: series.opcounter_insert ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
          <MiniChart
            title="Update"
            series={[
              {
                name: "opcounter_update",
                color: "#f472b6",
                points: series.opcounter_update ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
          <MiniChart
            title="Delete"
            series={[
              {
                name: "opcounter_delete",
                color: "#fb7185",
                points: series.opcounter_delete ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
        </div>
      </div>

      {/* ─ Memory + Storage + Latency + Disk Queue ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          스토리지 / 메모리 / 레이턴시
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
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
          <MiniChart
            title="Read / Write Latency"
            series={[
              {
                name: "read",
                color: "#60a5fa",
                points: series.read_latency_ms ?? [],
              },
              {
                name: "write",
                color: "#f472b6",
                points: series.write_latency_ms ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="ms"
          />
          <MiniChart
            title="Disk Queue Depth"
            series={[
              {
                name: "disk_queue_depth",
                color: "#fbbf24",
                points: series.disk_queue_depth ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
          <MiniChart
            title="Storage"
            series={[
              {
                name: "storage_bytes",
                color: "#a78bfa",
                points: (series.storage_bytes ?? []).map((p) => ({
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
