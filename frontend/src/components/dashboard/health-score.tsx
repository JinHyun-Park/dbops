"use client";

import { useEffect, useState } from "react";
import { fetchBatchTimeseries } from "@/lib/api-client";
import { fmtExact, fmtNumber } from "@/lib/format";
import { MetricHint } from "@/components/design-system/metric-hint";
import { engineFamily } from "@/lib/engine";

interface Props {
  clusterId: string;
  engine?: string;
}

interface Signal {
  metric: string;
  label: string;
  threshold: number;
  weight: number;
  current: number;
  status: "ok" | "warn" | "crit";
  invert?: boolean;
}

interface SignalDef {
  metric: string;
  label: string;
  warn: number;
  crit: number;
  weight: number;
  transform?: (v: number) => number;
  // When true, LOW values are unhealthy (e.g. buffer-cache-hit ratio): status
  // is crit when current <= crit, warn when current <= warn. Default false
  // (HIGH is unhealthy). A signal with no datapoints is always "ok" — missing
  // data never penalizes the score (esp. important for inverted signals).
  invert?: boolean;
}

const SIGNALS_RELATIONAL: SignalDef[] = [
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

// DocumentDB: CPU / replica lag / cursor timeouts (HIGH bad) + buffer cache hit
// (LOW bad → invert). Connection saturation is a ratio vs the instance limit,
// covered by the docdb_connection_saturation finding, so it's not a HealthScore
// signal here.
const SIGNALS_DOCUMENTDB: SignalDef[] = [
  { metric: "cpu_utilization", label: "CPU", warn: 70, crit: 90, weight: 35 },
  {
    metric: "replica_lag_ms",
    label: "Replica Lag",
    warn: 1000,
    crit: 5000,
    weight: 20,
  },
  {
    metric: "buffer_cache_hit",
    label: "Buffer Cache Hit",
    warn: 95,
    crit: 90,
    weight: 25,
    invert: true,
  },
  {
    metric: "cursors_timed_out",
    label: "Cursor Timeouts",
    warn: 1,
    crit: 10,
    weight: 20,
  },
];

// DynamoDB: throttles (any throttle is a problem) + per-op latency. Capacity
// over/under-provisioning is covered by the ddb_capacity_* findings.
const SIGNALS_DYNAMODB: SignalDef[] = [
  {
    metric: "read_throttle_events",
    label: "Read Throttles",
    warn: 1,
    crit: 10,
    weight: 30,
  },
  {
    metric: "write_throttle_events",
    label: "Write Throttles",
    warn: 1,
    crit: 10,
    weight: 30,
  },
  {
    metric: "throttled_requests",
    label: "Throttled Requests",
    warn: 1,
    crit: 10,
    weight: 20,
  },
  {
    metric: "latency_ms_getitem",
    label: "GetItem Latency",
    warn: 20,
    crit: 50,
    weight: 10,
  },
  {
    metric: "latency_ms_query",
    label: "Query Latency",
    warn: 20,
    crit: 50,
    weight: 10,
  },
];

// RDS instance (non-Aurora MySQL / SQL Server): only the metric_types that
// rds_instance_cw_collector.py actually writes. Aurora-only signals
// (replica_lag_ms, deadlocks, buffer_cache_hit) are NEVER collected for a
// standalone instance, so scoring against them left permanently blank rows.
// free_storage_bytes is LOW-bad (invert); the exhaustion ETA itself is covered
// by the capacity_forecast finding, this is just the "already tight" signal.
// transform normalizes the collected unit to the threshold unit: CloudWatch
// ReadLatency/WriteLatency are seconds, FreeStorageSpace is bytes.
const SIGNALS_RDS_INSTANCE: SignalDef[] = [
  { metric: "cpu", label: "CPU", warn: 70, crit: 90, weight: 30 },
  {
    metric: "db_connections",
    label: "Connections",
    warn: 100,
    crit: 200,
    weight: 20,
  },
  {
    metric: "free_storage_bytes",
    label: "Free Storage (GiB)",
    warn: 5,
    crit: 2,
    weight: 20,
    invert: true,
    transform: (v) => v / 1024 ** 3,
  },
  {
    metric: "read_latency",
    label: "Read Latency (ms)",
    warn: 20,
    crit: 50,
    weight: 15,
    transform: (v) => v * 1000,
  },
  {
    metric: "write_latency",
    label: "Write Latency (ms)",
    warn: 20,
    crit: 50,
    weight: 15,
    transform: (v) => v * 1000,
  },
];

// ElastiCache Redis/Valkey: metric_types from _REDIS_METRICS in
// elasticache_cw_collector.py. engine_cpu (EngineCPUUtilization) is the real
// saturation signal for the single-threaded engine thread, cache_cpu is the
// whole node. Connections cap is maxclients (65000 default), so the thresholds
// are far above the relational ones. Hit rate is derived (cache_hits /
// cache_misses), not a collected metric_type, so it stays in the overview panel.
const SIGNALS_ELASTICACHE_REDIS: SignalDef[] = [
  { metric: "engine_cpu", label: "Engine CPU", warn: 70, crit: 90, weight: 25 },
  {
    metric: "memory_usage_pct",
    label: "Memory Usage",
    warn: 80,
    crit: 90,
    weight: 25,
  },
  {
    metric: "evictions",
    label: "Evictions/min",
    warn: 1,
    crit: 100,
    weight: 20,
  },
  {
    metric: "curr_connections",
    label: "Connections",
    warn: 5000,
    crit: 20000,
    weight: 15,
  },
  {
    metric: "replication_lag",
    label: "Replication Lag (s)",
    warn: 5,
    crit: 30,
    weight: 15,
  },
];

// ElastiCache Memcached: _MEMCACHED_METRICS is a subset, with no
// EngineCPUUtilization, no DatabaseMemoryUsagePercentage and no replication.
// Memory pressure shows up as evictions plus swap (AWS guidance: keep SwapUsage
// under 50 MB).
const SIGNALS_ELASTICACHE_MEMCACHED: SignalDef[] = [
  { metric: "cache_cpu", label: "CPU", warn: 70, crit: 90, weight: 40 },
  {
    metric: "evictions",
    label: "Evictions/min",
    warn: 1,
    crit: 100,
    weight: 30,
  },
  {
    metric: "curr_connections",
    label: "Connections",
    warn: 5000,
    crit: 20000,
    weight: 15,
  },
  {
    metric: "swap_usage",
    label: "Swap (MB)",
    warn: 50,
    crit: 100,
    weight: 15,
    transform: (v) => v / 1024 ** 2,
  },
];

function signalsForEngine(engine?: string): SignalDef[] {
  const fam = engineFamily(engine);
  if (fam === "documentdb") return SIGNALS_DOCUMENTDB;
  if (fam === "dynamodb") return SIGNALS_DYNAMODB;
  if (fam === "rds_instance") return SIGNALS_RDS_INSTANCE;
  if (fam === "elasticache") {
    // Registry engine is the AWS-reported "redis" | "valkey" | "memcached".
    return (engine || "").toLowerCase().includes("memcached")
      ? SIGNALS_ELASTICACHE_MEMCACHED
      : SIGNALS_ELASTICACHE_REDIS;
  }
  return SIGNALS_RELATIONAL;
}

export function HealthScore({ clusterId, engine }: Props) {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const signalDefs = signalsForEngine(engine);
    const load = async () => {
      try {
        const d = await fetchBatchTimeseries(
          clusterId,
          signalDefs.map((s) => s.metric),
          1,
        );
        const results: Signal[] = signalDefs.map((s) => {
          const points = d.series[s.metric] || [];
          const hasData = points.length > 0;
          const raw = hasData
            ? Number(points[points.length - 1].value) || 0
            : 0;
          // transform normalizes the collected unit to the threshold/display
          // unit (seconds to ms, bytes to GiB) so the row never renders a
          // rounded-to-zero latency or an ambiguous raw byte count.
          const current = s.transform ? s.transform(raw) : raw;
          // Missing data → "ok": never penalize the score for an unpublished
          // metric (critical for inverted signals, where current=0 would
          // otherwise read as crit).
          let status: Signal["status"] = "ok";
          if (hasData) {
            status = s.invert
              ? current <= s.crit
                ? "crit"
                : current <= s.warn
                  ? "warn"
                  : "ok"
              : current >= s.crit
                ? "crit"
                : current >= s.warn
                  ? "warn"
                  : "ok";
          }
          return {
            metric: s.metric,
            label: s.label,
            threshold: s.warn,
            weight: s.weight,
            current,
            status,
            invert: s.invert,
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
  }, [clusterId, engine]);

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
                title={`${fmtExact(s.current)} (warn ${s.invert ? "≤" : "≥"} ${
                  s.threshold
                })`}
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
