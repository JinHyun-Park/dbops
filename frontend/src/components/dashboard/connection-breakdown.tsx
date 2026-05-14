"use client";

import { useEffect, useState } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
  ReferenceLine,
} from "recharts";
import { fetchBatchTimeseries, fetchClusterSettings } from "@/lib/api-client";
import { fmtExact, fmtNumber } from "@/lib/format";

interface Point {
  ts: string;
  value: number | string;
}

const STATES = [
  { metric: "conn_active", label: "active", color: "#34d399" },
  { metric: "conn_idle", label: "idle", color: "#60a5fa" },
  { metric: "conn_idle_in_tx", label: "idle in tx", color: "#fbbf24" },
  {
    metric: "conn_idle_in_tx_aborted",
    label: "idle aborted",
    color: "#f472b6",
  },
];

function fmtTime(iso: string) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

export function ConnectionBreakdown({
  clusterId,
  hours = 1,
}: {
  clusterId: string;
  hours?: number;
}) {
  const [data, setData] = useState<Record<string, number | string>[]>([]);
  const [loading, setLoading] = useState(true);
  const [maxConn, setMaxConn] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchClusterSettings(clusterId)
      .then((d) => {
        if (cancelled) return;
        const m = (d.settings || []).find(
          (s: { name: string }) => s.name === "max_connections",
        );
        if (m) setMaxConn(Number(m.value));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [clusterId]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await fetchBatchTimeseries(
          clusterId,
          STATES.map((s) => s.metric),
          hours,
        );
        const tsMap = new Map<string, Record<string, number | string>>();
        for (const state of STATES) {
          for (const p of (d.series[state.metric] || []) as Point[]) {
            const key = fmtTime(p.ts);
            const row = tsMap.get(key) || { ts: key };
            row[state.label] = Number(p.value) || 0;
            tsMap.set(key, row);
          }
        }
        const ordered = Array.from(tsMap.values()).sort((a, b) =>
          (a.ts as string).localeCompare(b.ts as string),
        );
        if (!cancelled) {
          setData(ordered);
          setLoading(false);
        }
      } catch {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    const iv = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId, hours]);

  const latest = data[data.length - 1] || {};
  const total = STATES.reduce((s, x) => s + (Number(latest[x.label]) || 0), 0);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4 col-span-full">
      <div className="flex items-baseline justify-between mb-3">
        <div className="text-xs text-zinc-400 uppercase tracking-wider">
          Connection Activity Breakdown
        </div>
        <div className="text-xs text-zinc-500">
          total:{" "}
          <span
            className="text-zinc-300 font-mono tabular-nums"
            title={fmtExact(total)}
          >
            {fmtExact(total)}
          </span>
          {maxConn !== null && (
            <>
              {" / "}
              <span
                className="text-zinc-300 font-mono tabular-nums"
                title={fmtExact(maxConn)}
              >
                {fmtExact(maxConn)}
              </span>
              {" ("}
              <span
                className={
                  total / maxConn > 0.8
                    ? "text-rose-400 font-mono tabular-nums"
                    : total / maxConn > 0.6
                      ? "text-amber-400 font-mono tabular-nums"
                      : "text-emerald-400 font-mono tabular-nums"
                }
                title={`${total} of ${maxConn} max_connections`}
              >
                {((total / maxConn) * 100).toFixed(0)}%
              </span>
              {")"}
            </>
          )}
        </div>
      </div>
      <div className="flex gap-4 mb-3 text-xs">
        {STATES.map((s) => {
          const v = Number(latest[s.label]) || 0;
          return (
            <div key={s.metric} className="flex items-center gap-1.5">
              <span
                className="w-2 h-2 rounded-sm"
                style={{ background: s.color }}
              />
              <span className="text-zinc-400">{s.label}:</span>
              <span
                className="text-zinc-200 font-mono tabular-nums"
                title={fmtExact(v)}
              >
                {fmtNumber(v)}
              </span>
            </div>
          );
        })}
      </div>
      <div className="h-56">
        {loading ? (
          <div className="text-xs text-zinc-500 flex items-center h-full">
            Loading...
          </div>
        ) : data.length === 0 ? (
          <div className="text-xs text-zinc-500 flex items-center h-full">
            no connection data yet
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
              {maxConn !== null && (
                <ReferenceLine
                  y={maxConn}
                  stroke="#ef4444"
                  strokeDasharray="4 4"
                  label={{
                    value: `max ${maxConn}`,
                    fill: "#ef4444",
                    fontSize: 10,
                    position: "right",
                  }}
                />
              )}
              {STATES.map((s) => (
                <Area
                  key={s.metric}
                  type="monotone"
                  dataKey={s.label}
                  stackId="1"
                  stroke={s.color}
                  fill={s.color}
                  fillOpacity={0.7}
                />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
