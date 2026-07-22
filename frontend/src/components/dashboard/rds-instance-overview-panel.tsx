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
} from "recharts";
import {
  fetchResourceDetails,
  fetchBatchTimeseries,
  type TimeRange,
} from "@/lib/api-client";
import { Expandable } from "@/components/design-system/expandable";
import { fmtDecimal, fmtBytes } from "@/lib/format";
import { useChartColors } from "@/lib/use-chart-colors";

type Point = { ts: string; value: number | string; dimensions?: string | null };

// RDS instance resource_details — MUST match the collector's JSON keys
// (rds_instance_cw_collector.py builds this dict; 3-tier parity).
interface RdsInstanceDetails {
  instance_class?: string;
  multi_az?: boolean;
  storage_type?: string;
  allocated_storage_gb?: number;
  license_model?: string;
  publicly_accessible?: boolean;
  pi_enabled?: boolean;
  endpoint?: string;
  port?: number;
}

const METRICS = [
  "cpu",
  "db_connections",
  "freeable_memory",
  "free_storage_bytes",
] as const;

type Metric = (typeof METRICS)[number];

function fmtTime(iso: string) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

// ─── Stat tile ───────────────────────────────────────────────────────────────

function StatTile({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="bg-zinc-950 border border-zinc-800 rounded p-3">
      <div className="text-[11px] text-zinc-500 mb-1">{label}</div>
      <div className="text-sm font-mono text-zinc-100">{value}</div>
    </div>
  );
}

function boolTile(v: boolean | undefined) {
  if (v === undefined) return "—";
  return (
    <span className={v ? "text-emerald-400" : "text-zinc-500"}>
      {v ? "Yes" : "No"}
    </span>
  );
}

// ─── Mini timeseries chart card ─────────────────────────────────────────────

function MiniChart({
  title,
  points,
  loading,
  colors,
  color,
  unit,
  type = "line",
  formatValue,
}: {
  title: string;
  points: Point[];
  loading: boolean;
  colors: ReturnType<typeof useChartColors>;
  color: string;
  unit?: string;
  type?: "line" | "area";
  formatValue?: (v: number) => string;
}) {
  const data = points.map((p) => ({
    ts: fmtTime(p.ts),
    value: Number(p.value) || 0,
  }));
  const current = points.length
    ? Number(points[points.length - 1].value) || 0
    : 0;
  const displayCurrent = formatValue
    ? formatValue(current)
    : fmtDecimal(current, 2);

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
                  <Area
                    type="monotone"
                    dataKey="value"
                    stroke={color}
                    fill={color}
                    fillOpacity={0.15}
                    dot={false}
                  />
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
                  <Line
                    type="monotone"
                    dataKey="value"
                    stroke={color}
                    strokeWidth={2}
                    dot={false}
                  />
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

export function RdsInstanceOverviewPanel({
  clusterId,
  range,
  engineVersion,
}: {
  clusterId: string;
  range: TimeRange;
  // resource_details (fetchResourceDetails) has no engine_version for the
  // rds_instance family — the handler only merges that column into
  // resource_details for DocumentDB. The caller already has it in scope via
  // the /overview response (cluster_meta.engine_version, selected universally
  // by `SELECT *`), so it's passed down instead of re-fetched.
  engineVersion?: string;
}) {
  const chart = useChartColors();
  const [details, setDetails] = useState<RdsInstanceDetails | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(true);
  const [detailsError, setDetailsError] = useState(false);
  const [series, setSeries] = useState<Record<Metric, Point[]>>(
    {} as Record<Metric, Point[]>,
  );
  const [seriesLoading, setSeriesLoading] = useState(true);
  const [seriesError, setSeriesError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // reqSeq: only the most recently *started* request may write state —
    // guards against an out-of-order settle (distinct from `cancelled`,
    // which guards against a stale effect instance after unmount/re-run).
    let reqSeq = 0;
    // Clear the previous cluster's details up front so a slow or failed
    // refetch can't leave stale data rendering under the new clusterId.
    setDetails(null);
    setDetailsError(false);
    const id = ++reqSeq;
    Promise.resolve().then(() => {
      if (cancelled) return;
      setDetailsLoading(true);
      fetchResourceDetails(clusterId)
        .then((d) => {
          if (cancelled || id !== reqSeq) return;
          setDetails((d.resource_details as RdsInstanceDetails) ?? null);
          setDetailsLoading(false);
        })
        .catch(() => {
          if (cancelled || id !== reqSeq) return;
          setDetails(null);
          setDetailsError(true);
          setDetailsLoading(false);
        });
    });
    return () => {
      cancelled = true;
    };
  }, [clusterId]);

  useEffect(() => {
    let cancelled = false;
    // reqSeq: the initial load and each 30s poll tick race independently —
    // if the initial request is slow and a later poll tick settles first,
    // the initial request's late arrival must not overwrite the fresher
    // data with stale success or a stale error. Only the request whose id
    // still matches the latest-started one may write state.
    let reqSeq = 0;
    // Clear stale series from the previous cluster/range up front — without
    // this, a switch to a new cluster keeps rendering the old cluster's
    // charts (unmasked, no loading indicator) until the new fetch resolves.
    setSeries({} as Record<Metric, Point[]>);
    setSeriesLoading(true);
    setSeriesError(false);
    // ponytail: only the first load (per clusterId/range) clears on failure
    // and surfaces an error card; later 30s poll failures on the same
    // cluster keep showing the last-good data and retry silently — a
    // transient blip shouldn't blank out charts the DBA is actively reading.
    const load = (isInitial: boolean) => {
      const id = ++reqSeq;
      fetchBatchTimeseries(clusterId, [...METRICS], range)
        .then((d) => {
          if (cancelled || id !== reqSeq) return;
          setSeries((d.series || {}) as Record<Metric, Point[]>);
          setSeriesError(false);
          setSeriesLoading(false);
        })
        .catch(() => {
          if (cancelled || id !== reqSeq) return;
          if (isInitial) {
            setSeries({} as Record<Metric, Point[]>);
            setSeriesError(true);
          }
          setSeriesLoading(false);
        });
    };
    load(true);
    const iv = setInterval(() => load(false), 30_000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId, range]);

  const storageLabel =
    details?.storage_type && details?.allocated_storage_gb != null
      ? `${details.storage_type} · ${fmtDecimal(
          details.allocated_storage_gb,
          0,
        )} GiB`
      : "—";

  return (
    <div className="space-y-6">
      {/* ─ Resource details tiles ─ */}
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        <div className="text-sm text-zinc-200 font-medium mb-3">
          인스턴스 개요
        </div>
        {detailsLoading ? (
          <div className="text-zinc-500 text-sm">불러오는 중…</div>
        ) : detailsError ? (
          <div className="text-rose-300 text-sm">
            인스턴스 상세 정보를 불러오지 못했습니다.
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-3">
            <StatTile
              label="Instance Class"
              value={details?.instance_class ?? "—"}
            />
            <StatTile label="Engine Version" value={engineVersion || "—"} />
            <StatTile label="Multi-AZ" value={boolTile(details?.multi_az)} />
            <StatTile label="Storage" value={storageLabel} />
            <StatTile label="License" value={details?.license_model ?? "—"} />
            <StatTile
              label="Performance Insights"
              value={boolTile(details?.pi_enabled)}
            />
          </div>
        )}
      </div>

      {/* ─ Charts ─ */}
      <div>
        <div className="text-[10px] uppercase tracking-[0.15em] text-zinc-500 mb-3">
          리소스 사용률 (Resource Usage)
        </div>
        {seriesError ? (
          <div className="text-rose-300 text-sm bg-zinc-900/50 border border-zinc-800 p-5">
            메트릭을 불러오지 못했습니다. 잠시 후 다시 시도합니다.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <MiniChart
              title="CPU Utilization"
              points={series.cpu ?? []}
              loading={seriesLoading}
              colors={chart}
              color="#34d399"
              unit="%"
              type="area"
              formatValue={(v) => v.toFixed(1)}
            />
            <MiniChart
              title="Connections"
              points={series.db_connections ?? []}
              loading={seriesLoading}
              colors={chart}
              color="#f472b6"
              type="area"
              formatValue={(v) => fmtDecimal(v, 0)}
            />
            <MiniChart
              title="Freeable Memory"
              points={series.freeable_memory ?? []}
              loading={seriesLoading}
              colors={chart}
              color="#22d3ee"
              type="area"
              formatValue={(v) => fmtBytes(v)}
            />
            <MiniChart
              title="Free Storage"
              points={series.free_storage_bytes ?? []}
              loading={seriesLoading}
              colors={chart}
              color="#60a5fa"
              type="area"
              formatValue={(v) => fmtBytes(v)}
            />
          </div>
        )}
      </div>
    </div>
  );
}
