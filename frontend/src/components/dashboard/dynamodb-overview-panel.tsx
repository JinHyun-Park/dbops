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
interface DdbKeyAttr {
  name: string;
  type: string; // S | N | B
}

interface DdbGsi {
  name: string;
  partition_key?: DdbKeyAttr | null;
  sort_key?: DdbKeyAttr | null;
  projection?: string | null;
  projection_attrs?: string[] | null;
  status?: string | null;
  item_count?: number | null;
  size_bytes?: number | null;
  // legacy shape fallback
  index_name?: string;
}

interface DdbLsi {
  name: string;
  partition_key?: DdbKeyAttr | null;
  sort_key?: DdbKeyAttr | null;
  projection?: string | null;
  projection_attrs?: string[] | null;
}

interface DdbKeySchema {
  partition_key?: DdbKeyAttr | null;
  sort_key?: DdbKeyAttr | null;
}

interface DdbDetails {
  billing_mode?: string | null;
  item_count?: number | null;
  table_size_bytes?: number | null;
  table_status?: string | null;
  key_schema?: DdbKeySchema | null;
  // Accept new rich-object shape AND legacy string[] / {index_name} shape
  gsi?: Array<string | DdbGsi> | null;
  lsi?: Array<DdbLsi> | null;
}

/** Normalise a GSI entry regardless of which shape the collector stored. */
function normaliseGsi(g: string | DdbGsi): DdbGsi {
  if (typeof g === "string") return { name: g };
  return { ...g, name: g.name ?? g.index_name ?? "(unnamed)" };
}

function fmtTime(iso: string) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

// ─── Key type badge ──────────────────────────────────────────────────────────

function TypeBadge({ type }: { type: string }) {
  return (
    <span className="ml-1.5 inline-flex items-center px-1.5 py-0 rounded text-[10px] font-mono font-semibold bg-zinc-800 text-zinc-300 border border-zinc-700 leading-5">
      {type}
    </span>
  );
}

function KeyRow({ label, attr }: { label: string; attr?: DdbKeyAttr | null }) {
  return (
    <div className="flex items-center gap-2 py-1">
      <span className="text-[11px] text-zinc-500 w-24 shrink-0">{label}</span>
      {attr ? (
        <span className="text-sm font-mono text-zinc-100">
          {attr.name}
          {attr.type && <TypeBadge type={attr.type} />}
        </span>
      ) : (
        <span className="text-sm text-zinc-600">없음</span>
      )}
    </div>
  );
}

// ─── Projection label ─────────────────────────────────────────────────────────

function projectionLabel(
  projection?: string | null,
  attrs?: string[] | null,
): string {
  if (!projection) return "—";
  if (projection === "INCLUDE" && attrs && attrs.length > 0) {
    return `INCLUDE (${attrs.join(", ")})`;
  }
  return projection;
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

  const gsiList = (details?.gsi ?? []).map(normaliseGsi);
  const lsiList = details?.lsi ?? [];
  const keySchema = details?.key_schema ?? null;

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
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
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
              label="Table Status"
              value={details?.table_status ?? "—"}
            />
          </div>
        )}
      </div>

      {/* ─ Key Schema ─ */}
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        <div className="text-sm text-zinc-200 font-medium mb-3">Key Schema</div>
        {detailsLoading ? (
          <div className="text-zinc-500 text-sm">불러오는 중…</div>
        ) : (
          <div className="divide-y divide-zinc-800/60">
            <KeyRow
              label="Partition Key"
              attr={keySchema?.partition_key ?? null}
            />
            <KeyRow label="Sort Key" attr={keySchema?.sort_key ?? null} />
          </div>
        )}
      </div>

      {/* ─ Global Secondary Indexes ─ */}
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        <div className="flex items-center justify-between mb-3">
          <div className="text-sm text-zinc-200 font-medium">
            Global Secondary Indexes
          </div>
          <div className="text-[11px] text-zinc-500 font-mono">
            {gsiList.length}개
          </div>
        </div>
        {detailsLoading ? (
          <div className="text-zinc-500 text-sm">불러오는 중…</div>
        ) : gsiList.length === 0 ? (
          <div className="text-sm text-zinc-600">GSI 없음</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[12px]">
              <thead>
                <tr className="border-b border-zinc-800">
                  <th className="text-left text-[10px] uppercase tracking-wider text-zinc-500 pb-2 pr-4 font-medium">
                    Index
                  </th>
                  <th className="text-left text-[10px] uppercase tracking-wider text-zinc-500 pb-2 pr-4 font-medium">
                    Keys
                  </th>
                  <th className="text-left text-[10px] uppercase tracking-wider text-zinc-500 pb-2 pr-4 font-medium">
                    Projection
                  </th>
                  <th className="text-left text-[10px] uppercase tracking-wider text-zinc-500 pb-2 pr-4 font-medium">
                    Status
                  </th>
                  <th className="text-right text-[10px] uppercase tracking-wider text-zinc-500 pb-2 font-medium">
                    Items / Size
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800/50">
                {gsiList.map((g) => (
                  <tr key={g.name} className="group">
                    <td className="py-2 pr-4 font-mono text-zinc-200 align-top">
                      {g.name}
                    </td>
                    <td className="py-2 pr-4 align-top">
                      <div className="flex flex-col gap-0.5">
                        {g.partition_key ? (
                          <span className="text-zinc-300">
                            <span className="text-zinc-500 mr-1">PK:</span>
                            {g.partition_key.name}
                            {g.partition_key.type && (
                              <TypeBadge type={g.partition_key.type} />
                            )}
                          </span>
                        ) : null}
                        {g.sort_key ? (
                          <span className="text-zinc-300">
                            <span className="text-zinc-500 mr-1">SK:</span>
                            {g.sort_key.name}
                            {g.sort_key.type && (
                              <TypeBadge type={g.sort_key.type} />
                            )}
                          </span>
                        ) : null}
                        {!g.partition_key && !g.sort_key && (
                          <span className="text-zinc-600">—</span>
                        )}
                      </div>
                    </td>
                    <td className="py-2 pr-4 text-zinc-400 align-top max-w-[200px] break-words">
                      {projectionLabel(g.projection, g.projection_attrs)}
                    </td>
                    <td className="py-2 pr-4 align-top">
                      <span
                        className={
                          g.status === "ACTIVE"
                            ? "text-emerald-400"
                            : "text-zinc-400"
                        }
                      >
                        {g.status ?? "—"}
                      </span>
                    </td>
                    <td className="py-2 text-right text-zinc-400 align-top whitespace-nowrap">
                      {g.item_count != null ? (
                        <span>
                          {fmtExact(g.item_count)}
                          {g.size_bytes != null && (
                            <span className="text-zinc-600 ml-1">
                              / {fmtBytes(g.size_bytes)}
                            </span>
                          )}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ─ Local Secondary Indexes ─ */}
      {(detailsLoading || lsiList.length > 0) && (
        <div className="bg-zinc-900/50 border border-zinc-800 p-5">
          <div className="flex items-center justify-between mb-3">
            <div className="text-sm text-zinc-200 font-medium">
              Local Secondary Indexes
            </div>
            {!detailsLoading && (
              <div className="text-[11px] text-zinc-500 font-mono">
                {lsiList.length}개
              </div>
            )}
          </div>
          {detailsLoading ? (
            <div className="text-zinc-500 text-sm">불러오는 중…</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[12px]">
                <thead>
                  <tr className="border-b border-zinc-800">
                    <th className="text-left text-[10px] uppercase tracking-wider text-zinc-500 pb-2 pr-4 font-medium">
                      Index
                    </th>
                    <th className="text-left text-[10px] uppercase tracking-wider text-zinc-500 pb-2 pr-4 font-medium">
                      Sort Key
                    </th>
                    <th className="text-left text-[10px] uppercase tracking-wider text-zinc-500 pb-2 font-medium">
                      Projection
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {lsiList.map((l) => (
                    <tr key={l.name}>
                      <td className="py-2 pr-4 font-mono text-zinc-200 align-top">
                        {l.name}
                      </td>
                      <td className="py-2 pr-4 align-top">
                        {l.sort_key ? (
                          <span className="text-zinc-300">
                            <span className="text-zinc-500 mr-1">SK:</span>
                            {l.sort_key.name}
                            {l.sort_key.type && (
                              <TypeBadge type={l.sort_key.type} />
                            )}
                          </span>
                        ) : (
                          <span className="text-zinc-600">—</span>
                        )}
                      </td>
                      <td className="py-2 text-zinc-400 align-top max-w-[200px] break-words">
                        {projectionLabel(l.projection, l.projection_attrs)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

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
