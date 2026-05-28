"use client";

import { useEffect, useState } from "react";
import {
  AreaChart,
  Area,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from "recharts";
import { fetchTimeseries } from "@/lib/api-client";
import { Expandable } from "@/components/design-system/expandable";
import { fmtDecimal } from "@/lib/format";

type Point = { ts: string; value: number | string; dimensions?: string };

interface Props {
  clusterId: string;
  metric: string;
  title: string;
  unit?: string;
  hours?: number;
  color?: string;
  type?: "line" | "area" | "stacked";
  formatValue?: (v: number) => string;
  // When provided, the chart uses these points instead of fetching its own.
  // Lets the parent batch-fetch all metrics in a single API call.
  externalPoints?: Point[];
  externalLoading?: boolean;
}

// Wait-event series palette — 10 hues that read clearly on the dark zinc
// background. The light variant uses the matching -700/-800 tier of each
// hue so all series stay above 4.5:1 against the cream + white surfaces.
const WAIT_COLORS_DARK = [
  "#60a5fa",
  "#f472b6",
  "#fbbf24",
  "#34d399",
  "#a78bfa",
  "#fb7185",
  "#22d3ee",
  "#facc15",
  "#fb923c",
  "#94a3b8",
];
const WAIT_COLORS_LIGHT = [
  "#1d4ed8", // blue-700
  "#9d174d", // pink-800
  "#92400e", // amber-800
  "#065f46", // emerald-800
  "#5b21b6", // violet-800
  "#9f1239", // rose-800
  "#0e7490", // cyan-700
  "#854d0e", // yellow-800
  "#9a3412", // orange-800
  "#475569", // slate-600
];

function useWaitColors(): string[] {
  const [light, setLight] = useState(false);
  useEffect(() => {
    const sync = () =>
      setLight(document.documentElement.dataset.theme === "light");
    sync();
    const obs = new MutationObserver(sync);
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => obs.disconnect();
  }, []);
  return light ? WAIT_COLORS_LIGHT : WAIT_COLORS_DARK;
}

function fmtTime(iso: string) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

export function TimeseriesChart({
  clusterId,
  metric,
  title,
  unit,
  hours = 1,
  color = "#60a5fa",
  type = "line",
  formatValue,
  externalPoints,
  externalLoading,
}: Props) {
  const usingExternal = externalPoints !== undefined;
  const [internalPoints, setInternalPoints] = useState<Point[]>([]);
  const [internalLoading, setInternalLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (usingExternal) return;
    let cancelled = false;
    const load = () => {
      fetchTimeseries(clusterId, metric, hours)
        .then((d) => {
          if (cancelled) return;
          setInternalPoints(d.points || []);
          setInternalLoading(false);
          setErr(null);
        })
        .catch((e) => {
          if (cancelled) return;
          setErr(e.message);
          setInternalLoading(false);
        });
    };
    load();
    const iv = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId, metric, hours, usingExternal]);

  const points = usingExternal ? externalPoints! : internalPoints;
  const loading = usingExternal ? Boolean(externalLoading) : internalLoading;

  if (type === "stacked") {
    return (
      <StackedAreaChart
        clusterId={clusterId}
        title={title}
        points={points}
        loading={loading}
        err={err}
        unit={unit}
      />
    );
  }

  const data = points.map((p) => ({
    ts: fmtTime(p.ts),
    value: Number(p.value) || 0,
  }));

  const current = data.length ? data[data.length - 1].value : 0;
  const max = data.reduce((m, d) => Math.max(m, d.value), 0);
  const display = formatValue ? formatValue(current) : fmtDecimal(current, 2);
  const displayMax = formatValue ? formatValue(max) : fmtDecimal(max, 2);

  return (
    <Expandable title={title}>
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        <div className="flex items-baseline justify-between mb-3 pr-8">
          <div className="text-sm text-zinc-200 font-medium">{title}</div>
          <div className="text-xs text-zinc-500">
            최대: {displayMax}
            {unit ? ` ${unit}` : ""}
          </div>
        </div>
        <div className="text-2xl font-semibold text-zinc-100 mb-3">
          {display}
          {unit && <span className="text-sm text-zinc-500 ml-1">{unit}</span>}
        </div>
        <div className="h-32">
          {loading ? (
            <div className="text-xs text-zinc-500 flex items-center h-full">
              불러오는 중…
            </div>
          ) : err ? (
            <div className="text-xs text-red-400 flex items-center h-full">
              {err}
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
                    stroke="#3f3f46"
                    vertical={false}
                  />
                  <XAxis dataKey="ts" stroke="#71717a" fontSize={10} />
                  <YAxis stroke="#71717a" fontSize={10} />
                  <Tooltip
                    contentStyle={{
                      background: "#18181b",
                      border: "1px solid #3f3f46",
                      fontSize: 12,
                    }}
                    labelStyle={{ color: "#a1a1aa" }}
                  />
                  <Area
                    type="monotone"
                    dataKey="value"
                    stroke={color}
                    fill={color}
                    fillOpacity={0.2}
                  />
                </AreaChart>
              ) : (
                <LineChart
                  data={data}
                  margin={{ top: 4, right: 4, bottom: 0, left: -20 }}
                >
                  <CartesianGrid
                    strokeDasharray="3 3"
                    stroke="#3f3f46"
                    vertical={false}
                  />
                  <XAxis dataKey="ts" stroke="#71717a" fontSize={10} />
                  <YAxis stroke="#71717a" fontSize={10} />
                  <Tooltip
                    contentStyle={{
                      background: "#18181b",
                      border: "1px solid #3f3f46",
                      fontSize: 12,
                    }}
                    labelStyle={{ color: "#a1a1aa" }}
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

function StackedAreaChart({
  title,
  points,
  loading,
  err,
  unit,
}: {
  clusterId: string;
  title: string;
  points: Point[];
  loading: boolean;
  err: string | null;
  unit?: string;
}) {
  const waitColors = useWaitColors();
  const grouped = new Map<string, Map<string, number>>();
  const eventSet = new Set<string>();

  for (const p of points) {
    let ev = "total";
    if (p.dimensions) {
      try {
        const d =
          typeof p.dimensions === "string"
            ? JSON.parse(p.dimensions)
            : p.dimensions;
        ev = d["db.wait_event.name"] || "CPU";
      } catch {
        ev = "CPU";
      }
    }
    eventSet.add(ev);
    const tsKey = fmtTime(p.ts);
    if (!grouped.has(tsKey)) grouped.set(tsKey, new Map());
    grouped
      .get(tsKey)!
      .set(ev, (grouped.get(tsKey)!.get(ev) || 0) + (Number(p.value) || 0));
  }

  const events = Array.from(eventSet).slice(0, 10);
  const data = Array.from(grouped.entries()).map(([ts, vals]) => {
    const row: Record<string, string | number> = { ts };
    let sum = 0;
    for (const ev of events) {
      const v = vals.get(ev) || 0;
      row[ev] = v;
      sum += v;
    }
    row._total = sum;
    return row;
  });

  const current = data.length ? Number(data[data.length - 1]._total) : 0;
  const max = data.reduce((m, d) => Math.max(m, Number(d._total) || 0), 0);

  return (
    <Expandable title={title} className="col-span-full">
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        <div className="flex items-baseline justify-between mb-3 pr-8">
          <div className="text-sm text-zinc-200 font-medium">{title}</div>
          <div className="text-xs text-zinc-500">
            최대: {fmtDecimal(max, 2)}
            {unit ? ` ${unit}` : ""}
          </div>
        </div>
        <div className="text-2xl font-semibold text-zinc-100 mb-3">
          {fmtDecimal(current, 2)}
          {unit && <span className="text-sm text-zinc-500 ml-1">{unit}</span>}
        </div>
        <div className="h-64">
          {loading ? (
            <div className="text-xs text-zinc-500 flex items-center h-full">
              불러오는 중…
            </div>
          ) : err ? (
            <div className="text-xs text-red-400 flex items-center h-full">
              {err}
            </div>
          ) : data.length === 0 ? (
            <div className="text-xs text-zinc-500 flex items-center h-full">
              데이터 없음
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={data}
                margin={{ top: 4, right: 4, bottom: 0, left: -20 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="#3f3f46"
                  vertical={false}
                />
                <XAxis dataKey="ts" stroke="#71717a" fontSize={10} />
                <YAxis stroke="#71717a" fontSize={10} />
                <Tooltip
                  contentStyle={{
                    background: "#18181b",
                    border: "1px solid #3f3f46",
                    fontSize: 12,
                  }}
                  labelStyle={{ color: "#a1a1aa" }}
                />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {events.map((ev, i) => (
                  <Area
                    key={ev}
                    type="monotone"
                    dataKey={ev}
                    stackId="1"
                    stroke={waitColors[i % waitColors.length]}
                    fill={waitColors[i % waitColors.length]}
                    fillOpacity={0.7}
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </Expandable>
  );
}
