"use client";

// High-resolution (~5s) active-session timeline from the ASH sampler — catches
// transient active/wait spikes the 5-min ETL misses. Reads active_session_samples
// via /active-sessions (its own table + 7d retention, separate from metrics).

import { useEffect, useState } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import {
  fetchActiveSessions,
  type ActiveSessionsResponse,
} from "@/lib/api-client";
import { useChartColors } from "@/lib/use-chart-colors";

function fmtTime(iso: string) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

export function ActiveSessionsPanel({ clusterId }: { clusterId: string }) {
  const colors = useChartColors();
  const [data, setData] = useState<ActiveSessionsResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchActiveSessions(clusterId, 1)
        .then((d) => {
          if (!cancelled) setData(d);
        })
        .catch(() => {
          if (!cancelled) setData(null);
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    setLoading(true);
    load();
    const t = setInterval(load, 30000); // refresh; sampler writes every ~5s
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [clusterId]);

  const samples = data?.samples ?? [];
  const chart = samples.map((s) => ({ ts: fmtTime(s.ts), active: s.active }));
  const latest = samples.length ? samples[samples.length - 1] : null;
  const peak = samples.reduce((m, s) => Math.max(m, s.active), 0);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-5">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h2 className="text-sm font-semibold text-zinc-200">
            활성 세션 (고해상 ~5초)
          </h2>
          <div className="text-[10px] text-zinc-500 mt-0.5">
            최근 1시간 · pg_stat_activity / processlist 5초 샘플 — 5분 ETL이
            놓치는 순간 스파이크 포착
          </div>
        </div>
        <div className="text-right">
          <div className="text-2xl font-semibold text-zinc-100">
            {latest ? latest.active : "—"}
          </div>
          <div className="text-[10px] text-zinc-500">
            현재 · peak {peak}
            {latest?.top_wait ? ` · ${latest.top_wait}` : ""}
          </div>
        </div>
      </div>
      <div className="h-40">
        {loading ? (
          <div className="text-xs text-zinc-500 flex items-center h-full">
            불러오는 중…
          </div>
        ) : chart.length === 0 ? (
          <div className="text-xs text-zinc-500 flex items-center h-full">
            샘플 없음 (샘플러 수집 대기)
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={chart}
              margin={{ top: 4, right: 4, bottom: 0, left: -20 }}
            >
              <CartesianGrid
                strokeDasharray="3 3"
                stroke={colors.grid}
                vertical={false}
              />
              <XAxis
                dataKey="ts"
                stroke={colors.axis}
                fontSize={10}
                minTickGap={40}
              />
              <YAxis stroke={colors.axis} fontSize={10} allowDecimals={false} />
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
                dataKey="active"
                stroke="#34d399"
                fill="#34d399"
                fillOpacity={0.15}
                dot={false}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
