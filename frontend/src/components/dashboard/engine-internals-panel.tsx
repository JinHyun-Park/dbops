"use client";

// Engine-internal signals the CloudWatch metrics don't expose — collected by the
// ETL collector from pg_stat_database / pg_stat_bgwriter (PostgreSQL) and
// SHOW ENGINE INNODB STATUS (MySQL). Read straight from metric_snapshots via the
// batch-timeseries endpoint (no per-metric backend allowlist).

import { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { fetchBatchTimeseries, type TimeRange } from "@/lib/api-client";
import { engineKind } from "@/lib/engine";
import { fmtDecimal } from "@/lib/format";
import { useChartColors } from "@/lib/use-chart-colors";

type Point = { ts: string; value: number | string; dimensions?: string };
type MetricDef = { key: string; title: string; unit: string };

const PG_METRICS: MetricDef[] = [
  {
    key: "pg_cache_hit_ratio",
    title: "캐시 히트율 (shared buffers)",
    unit: "%",
  },
  { key: "pg_rollback_ratio", title: "트랜잭션 롤백 비율", unit: "%" },
  {
    key: "pg_checkpoint_forced_ratio",
    title: "강제 체크포인트 비율",
    unit: "%",
  },
  { key: "pg_temp_bytes", title: "Temp 파일 스필 (누적)", unit: "B" },
];
const MYSQL_METRICS: MetricDef[] = [
  { key: "innodb_history_list_length", title: "History List Length", unit: "" },
  { key: "innodb_buffer_pool_hit_rate", title: "버퍼 풀 히트율", unit: "%" },
  { key: "innodb_pending_io", title: "대기 중 I/O", unit: "" },
  { key: "innodb_row_ops_per_sec", title: "Row Ops 처리량", unit: "/s" },
];
// SQL Server has no InnoDB/pg_stat equivalent — sys.dm_os_wait_stats is
// collected as a single metric_type dimensioned by wait_type (see
// data-pipeline/rds_direct_collector/mssql_waits.py), not flat MetricDefs.
const MSSQL_WAIT_METRIC = "mssql_wait_ms";

function fmtTime(iso: string) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

// mssql_wait_ms is one metric_type dimensioned by wait_type (multiple rows
// per timestamp bucket) — not a flat per-key series like MYSQL_METRICS/
// PG_METRICS, so it gets a ranked bar list instead of a MiniChart grid.
function latestWaitsByType(points: Point[]) {
  const latest = new Map<string, { ts: string; value: number }>();
  for (const p of points) {
    let waitType = "unknown";
    if (p.dimensions) {
      try {
        waitType = JSON.parse(p.dimensions).wait_type ?? waitType;
      } catch {
        // malformed dimensions JSON — keep "unknown" bucket
      }
    }
    const value = Number(p.value) || 0;
    const cur = latest.get(waitType);
    if (!cur || p.ts > cur.ts) latest.set(waitType, { ts: p.ts, value });
  }
  return [...latest.entries()]
    .map(([waitType, { value }]) => ({ waitType, value }))
    .sort((a, b) => b.value - a.value);
}

function TopWaitsCard({
  points,
  loading,
}: {
  points: Point[];
  loading: boolean;
}) {
  const waits = latestWaitsByType(points).slice(0, 8);
  const total = waits.reduce((s, w) => s + w.value, 0);
  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="text-sm text-zinc-200 font-medium mb-3">
        Top Waits (ms)
      </div>
      {loading ? (
        <div className="text-xs text-zinc-500">불러오는 중…</div>
      ) : waits.length === 0 ? (
        <div className="text-xs text-zinc-500">wait 데이터가 없어요</div>
      ) : (
        <div className="space-y-2">
          {waits.map((w) => {
            const pct = total > 0 ? (w.value / total) * 100 : 0;
            return (
              <div key={w.waitType}>
                <div className="flex justify-between text-xs mb-1">
                  <span
                    className="text-zinc-300 truncate max-w-[60%]"
                    title={w.waitType}
                  >
                    {w.waitType}
                  </span>
                  <span className="text-zinc-400 font-mono">
                    {fmtDecimal(w.value, 0)}{" "}
                    <span className="text-zinc-600">({pct.toFixed(1)}%)</span>
                  </span>
                </div>
                <div className="h-1.5 bg-zinc-900 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-sky-500"
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function MiniChart({
  def,
  points,
  loading,
  colors,
}: {
  def: MetricDef;
  points: Point[];
  loading: boolean;
  colors: ReturnType<typeof useChartColors>;
}) {
  const data = points.map((p) => ({
    ts: fmtTime(p.ts),
    v: Number(p.value) || 0,
  }));
  const current = points.length
    ? Number(points[points.length - 1].value) || 0
    : null;
  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-sm text-zinc-200 font-medium">{def.title}</div>
        {def.unit && (
          <div className="text-[10px] text-zinc-500 uppercase tracking-wider">
            {def.unit}
          </div>
        )}
      </div>
      <div className="text-2xl font-semibold text-zinc-100 mb-2">
        {current == null ? "—" : fmtDecimal(current, 2)}
        {current != null && def.unit && (
          <span className="text-sm text-zinc-500 ml-1">{def.unit}</span>
        )}
      </div>
      <div className="h-24">
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
                dataKey="v"
                stroke="#38bdf8"
                strokeWidth={2}
                dot={false}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}

export function EngineInternalsPanel({
  clusterId,
  engine,
  range,
}: {
  clusterId: string;
  engine?: string;
  range: TimeRange;
}) {
  const colors = useChartColors();
  const kind = engineKind(engine);
  const mysql = kind === "mysql";
  const sqlserver = kind === "sqlserver";
  const defs = mysql ? MYSQL_METRICS : sqlserver ? [] : PG_METRICS;
  const metricKeys = sqlserver ? [MSSQL_WAIT_METRIC] : defs.map((d) => d.key);
  const [series, setSeries] = useState<Record<string, Point[]>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchBatchTimeseries(clusterId, metricKeys, range)
      .then((d) => {
        if (!cancelled) setSeries((d.series || {}) as Record<string, Point[]>);
      })
      .catch(() => {
        if (!cancelled) setSeries({});
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterId, range, kind]);

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <h2 className="text-sm font-semibold text-zinc-300">엔진 내부 지표</h2>
        <span className="text-[10px] text-zinc-500">
          {mysql
            ? "InnoDB engine status"
            : sqlserver
              ? "sys.dm_os_wait_stats"
              : "pg_stat_database / bgwriter"}
        </span>
      </div>
      {sqlserver ? (
        <TopWaitsCard
          points={series[MSSQL_WAIT_METRIC] || []}
          loading={loading}
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          {defs.map((def) => (
            <MiniChart
              key={def.key}
              def={def}
              points={series[def.key] || []}
              loading={loading}
              colors={colors}
            />
          ))}
        </div>
      )}
    </div>
  );
}
