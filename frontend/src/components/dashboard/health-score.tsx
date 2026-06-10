"use client";

import { useEffect, useState } from "react";
import { fetchBatchTimeseries } from "@/lib/api-client";
import { fmtExact, fmtNumber } from "@/lib/format";
import { MetricHint } from "@/components/design-system/metric-hint";

interface Props {
  clusterId: string;
}

interface Signal {
  metric: string;
  label: string;
  threshold: number;
  weight: number;
  current: number;
  status: "ok" | "warn" | "crit";
}

const SIGNALS: {
  metric: string;
  label: string;
  warn: number;
  crit: number;
  weight: number;
  transform?: (v: number) => number;
}[] = [
  { metric: "cpu", label: "CPU", warn: 70, crit: 90, weight: 25 },
  { metric: "aas", label: "Load (AAS)", warn: 2, crit: 5, weight: 25 },
  {
    // Canonical total-connections metric = db_connections (CloudWatch
    // DatabaseConnections), collected for every cluster. The PI-only
    // "connections" (numbackends) was empty whenever Performance Insights
    // was off, which silently zeroed this signal.
    metric: "db_connections",
    label: "Connections",
    warn: 100,
    crit: 200,
    weight: 15,
  },
  {
    metric: "replica_lag_ms",
    label: "Replica Lag",
    warn: 1000,
    crit: 5000,
    weight: 15,
  },
  { metric: "deadlocks", label: "Deadlocks/min", warn: 1, crit: 5, weight: 10 },
  {
    metric: "read_iops",
    label: "Read IOPS",
    warn: 5000,
    crit: 10000,
    weight: 5,
  },
  {
    metric: "write_iops",
    label: "Write IOPS",
    warn: 5000,
    crit: 10000,
    weight: 5,
  },
];

export function HealthScore({ clusterId }: Props) {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const d = await fetchBatchTimeseries(
          clusterId,
          SIGNALS.map((s) => s.metric),
          1,
        );
        const results: Signal[] = SIGNALS.map((s) => {
          const points = d.series[s.metric] || [];
          const current = points.length
            ? Number(points[points.length - 1].value) || 0
            : 0;
          const status: Signal["status"] =
            current >= s.crit ? "crit" : current >= s.warn ? "warn" : "ok";
          return {
            metric: s.metric,
            label: s.label,
            threshold: s.warn,
            weight: s.weight,
            current,
            status,
          };
        });
        if (!cancelled) {
          setSignals(results);
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
  }, [clusterId]);

  const totalWeight = signals.reduce((s, x) => s + x.weight, 0);
  const score =
    signals.length === 0
      ? 100
      : Math.round(
          signals.reduce((sum, s) => {
            const w = s.status === "crit" ? 0 : s.status === "warn" ? 50 : 100;
            return sum + (w * s.weight) / totalWeight;
          }, 0),
        );

  // Grade by WORST SIGNAL, not by the weighted score alone. A low-weight
  // signal in crit (e.g. deadlocks, weight 10) only drops the score to 90,
  // which used to read "HEALTHY" right next to the CRITICAL incident banner —
  // an active deadlock storm must never grade green regardless of arithmetic.
  const hasCrit = signals.some((s) => s.status === "crit");
  const hasWarn = signals.some((s) => s.status === "warn");
  const grade = hasCrit
    ? {
        label: "CRITICAL",
        color: "text-rose-400",
        ring: "ring-rose-500/40",
        bg: "bg-rose-500/10",
      }
    : hasWarn || score < 90
      ? {
          label: "DEGRADED",
          color: "text-amber-400",
          ring: "ring-amber-500/40",
          bg: "bg-amber-500/10",
        }
      : {
          label: "HEALTHY",
          color: "text-emerald-400",
          ring: "ring-emerald-500/40",
          bg: "bg-emerald-500/10",
        };

  return (
    <div className={`bg-zinc-900/50 border border-zinc-800 p-5 ${grade.bg}`}>
      <div className="flex items-center gap-4 mb-4">
        <div
          className={`w-20 h-20 rounded-full ring-4 ${grade.ring} flex items-center justify-center bg-zinc-900`}
        >
          <span className={`text-2xl font-bold ${grade.color}`}>
            {loading ? "..." : score}
          </span>
        </div>
        <div>
          <div className="text-sm text-zinc-200 font-medium mb-1">
            Health Score
          </div>
          <div className={`text-lg font-semibold ${grade.color}`}>
            {grade.label}
          </div>
          <div className="text-xs text-zinc-500 mt-1">
            {signals.length} signals · weighted
          </div>
        </div>
      </div>
      <div className="space-y-1.5">
        {signals.map((s) => {
          const dot =
            s.status === "crit"
              ? "bg-rose-500"
              : s.status === "warn"
                ? "bg-amber-500"
                : "bg-emerald-500";
          return (
            <div
              key={s.metric}
              className="flex items-center justify-between text-xs"
            >
              <div className="flex items-center gap-2">
                <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
                <span className="text-zinc-300">{s.label}</span>
                <MetricHint metric={s.metric} />
              </div>
              <span
                className="text-zinc-400 font-mono tabular-nums"
                title={`${fmtExact(s.current)} (warn ≥ ${s.threshold})`}
              >
                {fmtNumber(s.current)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
