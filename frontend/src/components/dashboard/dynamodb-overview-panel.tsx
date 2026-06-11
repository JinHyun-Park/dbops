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
import { fetchResourceDetails, fetchBatchTimeseries } from "@/lib/api-client";
import { fmtDecimal, fmtBytes, fmtExact } from "@/lib/format";
import { useChartColors } from "@/lib/use-chart-colors";

type Point = { ts: string; value: number | string };

// DynamoDB-specific resource_details shape
interface DdbDetails {
  billing_mode?: string | null;
  item_count?: number | null;
  table_size_bytes?: number | null;
  gsi?: Array<{ index_name: string; [k: string]: unknown }> | null;
  table_status?: string | null;
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
  const allKeys = series.map((s) => s.name);
  // Merge all series into one flat data array keyed by time
  const timeMap = new Map<string, Record<string, number>>();
  for (const s of series) {
    for (const p of s.points) {
      const t = fmtTime(p.ts);
      if (!timeMap.has(t)) timeMap.set(t, { ts_key: 0 });
      timeMap.get(t)![s.name] = Number(p.value) || 0;
    }
  }
  const data = Array.from(timeMap.entries()).map(([t, vals]) => ({
    ts: t,
    ...vals,
  }));

  const currentVals = series.map((s) => {
    const pts = s.points;
    return pts.length ? Number(pts[pts.length - 1].value) || 0 : 0;
  });
  const primaryCurrent = currentVals[0] ?? 0;
  const displayCurrent = fmtDecimal(primaryCurrent, 2);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-5">
      <div className="flex items-baseline justify-between mb-3">
        <div className="text-sm text-zinc-200 font-medium">{title}</div>
        {unit && (
          <div className="text-[10px] text-zinc-500 uppercase tracking-wider">
            {unit}
          </div>
        )}
      </div>
      <div className="text-2xl font-semibold text-zinc-100 mb-3">
        {displayCurrent}
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
                {allKeys.length > 1 && (
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
                {allKeys.length > 1 && (
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
  );
}

// ─── Main panel ──────────────────────────────────────────────────────────────

const DDB_METRICS = [
  "consumed_rcu",
  "consumed_wcu",
  "provisioned_rcu",
  "provisioned_wcu",
  "read_throttle_events",
  "write_throttle_events",
  "throttled_requests",
  "latency_ms_getitem",
  "latency_ms_query",
  "latency_ms_putitem",
  "latency_ms_scan",
] as const;

type DdbMetric = (typeof DDB_METRICS)[number];

export function DynamodbOverviewPanel({ clusterId }: { clusterId: string }) {
  const chart = useChartColors();
  const [details, setDetails] = useState<DdbDetails | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(true);
  const [series, setSeries] = useState<Record<DdbMetric, Point[]>>(
    {} as Record<DdbMetric, Point[]>,
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
          setDetails((d.resource_details as DdbDetails) ?? null);
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
      fetchBatchTimeseries(clusterId, [...DDB_METRICS], 1)
        .then((d) => {
          if (cancelled) return;
          setSeries((d.series || {}) as Record<DdbMetric, Point[]>);
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
  }, [clusterId]);

  const isProvisioned =
    (details?.billing_mode ?? "").toUpperCase() === "PROVISIONED";

  const gsiList = details?.gsi ?? [];

  return (
    <div className="space-y-6">
      {/* ─ Resource details tiles ─ */}
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        <div className="text-sm text-zinc-200 font-medium mb-3">
          테이블 개요
        </div>
        {detailsLoading ? (
          <div className="text-zinc-500 text-sm">불러오는 중…</div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-3">
            <StatTile
              label="Billing Mode"
              value={details?.billing_mode ?? "—"}
            />
            <StatTile
              label="아이템 수"
              value={
                details?.item_count != null ? fmtExact(details.item_count) : "—"
              }
            />
            <StatTile
              label="테이블 크기"
              value={
                details?.table_size_bytes != null
                  ? fmtBytes(details.table_size_bytes)
                  : "—"
              }
            />
            <StatTile
              label="GSI 수"
              value={String(gsiList.length)}
              sub={
                gsiList.length > 0
                  ? gsiList.map((g) => g.index_name).join(", ")
                  : undefined
              }
            />
            <StatTile
              label="Table Status"
              value={details?.table_status ?? "—"}
            />
          </div>
        )}
      </div>

      {/* ─ Capacity charts ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          용량 (Capacity Units)
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <MiniChart
            title={
              isProvisioned ? "Consumed RCU vs Provisioned RCU" : "Consumed RCU"
            }
            series={
              isProvisioned
                ? [
                    {
                      name: "consumed_rcu",
                      color: "#60a5fa",
                      points: series.consumed_rcu ?? [],
                    },
                    {
                      name: "provisioned_rcu",
                      color: "#94a3b8",
                      points: series.provisioned_rcu ?? [],
                    },
                  ]
                : [
                    {
                      name: "consumed_rcu",
                      color: "#60a5fa",
                      points: series.consumed_rcu ?? [],
                    },
                  ]
            }
            loading={seriesLoading}
            colors={chart}
          />
          <MiniChart
            title={
              isProvisioned ? "Consumed WCU vs Provisioned WCU" : "Consumed WCU"
            }
            series={
              isProvisioned
                ? [
                    {
                      name: "consumed_wcu",
                      color: "#f472b6",
                      points: series.consumed_wcu ?? [],
                    },
                    {
                      name: "provisioned_wcu",
                      color: "#94a3b8",
                      points: series.provisioned_wcu ?? [],
                    },
                  ]
                : [
                    {
                      name: "consumed_wcu",
                      color: "#f472b6",
                      points: series.consumed_wcu ?? [],
                    },
                  ]
            }
            loading={seriesLoading}
            colors={chart}
          />
        </div>
      </div>

      {/* ─ Throttle charts ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          스로틀 이벤트 (Throttles)
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <MiniChart
            title="Read Throttle Events"
            series={[
              {
                name: "read_throttle_events",
                color: "#fb7185",
                points: series.read_throttle_events ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
          <MiniChart
            title="Write Throttle Events"
            series={[
              {
                name: "write_throttle_events",
                color: "#fb923c",
                points: series.write_throttle_events ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
          <MiniChart
            title="Throttled Requests"
            series={[
              {
                name: "throttled_requests",
                color: "#ef4444",
                points: series.throttled_requests ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
          />
        </div>
      </div>

      {/* ─ Latency charts ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          레이턴시 (Latency)
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <MiniChart
            title="GetItem Latency"
            series={[
              {
                name: "latency_ms_getitem",
                color: "#34d399",
                points: series.latency_ms_getitem ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="ms"
          />
          <MiniChart
            title="Query Latency"
            series={[
              {
                name: "latency_ms_query",
                color: "#22d3ee",
                points: series.latency_ms_query ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="ms"
          />
          <MiniChart
            title="PutItem Latency"
            series={[
              {
                name: "latency_ms_putitem",
                color: "#a78bfa",
                points: series.latency_ms_putitem ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="ms"
          />
          <MiniChart
            title="Scan Latency"
            series={[
              {
                name: "latency_ms_scan",
                color: "#fbbf24",
                points: series.latency_ms_scan ?? [],
              },
            ]}
            loading={seriesLoading}
            colors={chart}
            unit="ms"
          />
        </div>
      </div>
    </div>
  );
}
