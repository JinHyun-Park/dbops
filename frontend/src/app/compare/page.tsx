"use client";

import { useEffect, useMemo, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from "recharts";
import {
  fetchClusters,
  fetchBatchTimeseries,
  fetchClusterInstances,
  ClusterInstance,
} from "@/lib/api-client";
import { useChartColors } from "@/lib/use-chart-colors";
import { PageHeader, PageBody } from "@/components/design-system/page-shell";
import { Expandable } from "@/components/design-system/expandable";
import {
  engineBadge,
  engineFamily,
  isPostgres,
  FAMILY_META,
  ENGINE_GROUP_META,
  ENGINE_GROUP_ORDER,
} from "@/lib/engine";
import { groupByEngineGroup, displayName } from "@/lib/group-by-family";

interface ClusterRow {
  cluster_id: string;
  engine?: string;
  resource_name?: string;
}

type Mode = "cluster" | "period" | "instance";

interface MetricSpec {
  id: string;
  label: string;
  unit?: string;
  pgOnly?: boolean;
  fmt?: (v: number) => string;
}

// Subset of dashboard metrics — fits 2x3 grid cleanly and covers the signals
// a DBA usually compares (load, capacity, throughput).
const METRICS: MetricSpec[] = [
  { id: "cpu", label: "CPU %", unit: "%", fmt: (v) => v.toFixed(1) },
  { id: "aas", label: "Avg Active Sessions", fmt: (v) => v.toFixed(2) },
  { id: "db_connections", label: "Connections", fmt: (v) => v.toFixed(0) },
  { id: "read_iops", label: "Read IOPS", fmt: (v) => v.toFixed(0) },
  { id: "write_iops", label: "Write IOPS", fmt: (v) => v.toFixed(0) },
  {
    id: "replica_lag_ms",
    label: "Replica Lag",
    unit: "ms",
    fmt: (v) => v.toFixed(0),
  },
  {
    id: "xact_commit",
    label: "Tx / sec (PG)",
    pgOnly: true,
    fmt: (v) => v.toFixed(1),
  },
  {
    id: "tup_returned",
    label: "Tuples / sec (PG)",
    pgOnly: true,
    fmt: (v) => v.toFixed(0),
  },
];

const RANGE_OPTIONS = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 168 },
];

// Period offset preset for period-vs-period mode. The "B" series is the same
// window shifted back by (hours) so the two series align on relative time.
const PERIOD_SHIFT_LABEL: Record<number, string> = {
  1: "직전 1시간",
  6: "직전 6시간",
  24: "어제 같은 시간",
  168: "지난주 같은 시간",
};

interface SeriesPoint {
  ts: string;
  value: number | string;
}

function n(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

// Merge two time series into a single chart-friendly array. The "B" series is
// reindexed so points align with "A" by relative position (0, 5min, 10min, …)
// — only practical way to overlay last-week vs this-week, since absolute ts
// values differ by 7 days.
function mergeForChart(
  a: SeriesPoint[],
  b: SeriesPoint[],
  labelA: string,
  labelB: string,
): Array<Record<string, string | number>> {
  const len = Math.max(a.length, b.length);
  const out: Array<Record<string, string | number>> = [];
  for (let i = 0; i < len; i++) {
    const ap = a[i];
    const bp = b[i];
    const row: Record<string, string | number> = {
      // x axis uses the A timestamp so the most recent window controls the
      // time labels. If A is empty, fall back to B.
      ts: ap ? formatTs(ap.ts) : bp ? formatTs(bp.ts) : "",
    };
    if (ap) row[labelA] = n(ap.value);
    if (bp) row[labelB] = n(bp.value);
    out.push(row);
  }
  return out;
}

function formatTs(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

export default function ComparePage() {
  const [clusters, setClusters] = useState<ClusterRow[]>([]);
  const [mode, setMode] = useState<Mode>("cluster");
  const [hours, setHours] = useState(24);

  // Cluster-vs-cluster
  const [clusterA, setClusterA] = useState<string>("");
  const [clusterB, setClusterB] = useState<string>("");

  // Period-vs-period
  const [periodCluster, setPeriodCluster] = useState<string>("");

  // Instance-vs-instance
  const [instanceCluster, setInstanceCluster] = useState<string>("");
  const [instanceA, setInstanceA] = useState<string>("");
  const [instanceB, setInstanceB] = useState<string>("");
  const [clusterInstances, setClusterInstances] = useState<ClusterInstance[]>(
    [],
  );

  const [loadingA, setLoadingA] = useState(false);
  const [loadingB, setLoadingB] = useState(false);
  const [seriesA, setSeriesA] = useState<Record<string, SeriesPoint[]>>({});
  const [seriesB, setSeriesB] = useState<Record<string, SeriesPoint[]>>({});

  // Distinguish "the registry genuinely has <2 clusters" from "the list failed
  // to load" — the old code swallowed failures and showed the misleading
  // "register more clusters" banner with no way to retry short of a reload.
  const [clustersError, setClustersError] = useState(false);

  const loadClusters = () => {
    setClustersError(false);
    fetchClusters()
      .then((rows: ClusterRow[]) => {
        setClusters(rows);
        if (rows.length === 0) return;

        // Pick initial A (first cluster), then pick an initial B that is in the
        // same engine family as A. If none exists, fall back to A itself.
        setClusterA((cur) => {
          const a = cur || rows[0].cluster_id;
          // When setting A for the first time, also align B to the same family.
          if (!cur) {
            const aRow = rows.find((r) => r.cluster_id === a);
            const famA = engineFamily(aRow?.engine);
            setClusterB((curB) => {
              if (curB) {
                // Validate existing B is same family; clear if not.
                const bRow = rows.find((r) => r.cluster_id === curB);
                if (bRow && engineFamily(bRow.engine) !== famA) return a;
                return curB;
              }
              const sameFamily = rows.filter(
                (r) => engineFamily(r.engine) === famA,
              );
              return (
                sameFamily.find((r) => r.cluster_id !== a)?.cluster_id || a
              );
            });
          }
          return a;
        });
        setPeriodCluster((cur) => cur || rows[0].cluster_id);
        setInstanceCluster((cur) => cur || rows[0].cluster_id);
      })
      .catch((e) => {
        console.error("clusters fetch failed:", e);
        setClustersError(true);
      });
  };

  useEffect(() => {
    loadClusters();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Decide which engines we're comparing so we can hide PG-only metrics
  // when at least one side is MySQL.
  const engineA = clusters.find(
    (c) =>
      c.cluster_id ===
      (mode === "cluster"
        ? clusterA
        : mode === "instance"
          ? instanceCluster
          : periodCluster),
  )?.engine;
  const engineB = clusters.find((c) => c.cluster_id === clusterB)?.engine;
  const showPgOnly =
    mode === "cluster"
      ? isPostgres(engineA) && isPostgres(engineB)
      : isPostgres(engineA);

  // Detect cross-family state (e.g. loaded from a URL that had A=aurora, B=dynamodb).
  // When detected we show a notice and auto-clear B.
  const famA = engineFamily(engineA);
  const famB = engineFamily(engineB);
  const crossFamilyMismatch =
    mode === "cluster" &&
    !!clusterA &&
    !!clusterB &&
    clusterA !== clusterB &&
    famA !== famB;

  // Candidates for picker B: only same family as A.
  const candidatesB = useMemo(
    () => clusters.filter((c) => engineFamily(c.engine) === famA),
    [clusters, famA],
  );

  const visibleMetrics = useMemo(
    () => METRICS.filter((m) => !m.pgOnly || showPgOnly),
    [showPgOnly],
  );

  const metricIds = visibleMetrics.map((m) => m.id);
  const metricsKey = metricIds.join(",");

  // Cluster mode — fetch both clusters in parallel.
  useEffect(() => {
    if (mode !== "cluster" || !clusterA || !clusterB || metricIds.length === 0)
      return;
    let cancelled = false;
    setLoadingA(true);
    setLoadingB(true);
    Promise.allSettled([
      fetchBatchTimeseries(clusterA, metricIds, hours, 0),
      fetchBatchTimeseries(clusterB, metricIds, hours, 0),
    ]).then(([a, b]) => {
      if (cancelled) return;
      if (a.status === "fulfilled") setSeriesA(a.value.series || {});
      if (b.status === "fulfilled") setSeriesB(b.value.series || {});
      setLoadingA(false);
      setLoadingB(false);
    });
    return () => {
      cancelled = true;
    };
  }, [mode, clusterA, clusterB, hours, metricsKey]);

  // Period mode — fetch same cluster twice with different offsets.
  useEffect(() => {
    if (mode !== "period" || !periodCluster || metricIds.length === 0) return;
    let cancelled = false;
    setLoadingA(true);
    setLoadingB(true);
    Promise.allSettled([
      fetchBatchTimeseries(periodCluster, metricIds, hours, 0),
      fetchBatchTimeseries(periodCluster, metricIds, hours * 2, hours),
    ]).then(([cur, prev]) => {
      if (cancelled) return;
      if (cur.status === "fulfilled") setSeriesA(cur.value.series || {});
      if (prev.status === "fulfilled") setSeriesB(prev.value.series || {});
      setLoadingA(false);
      setLoadingB(false);
    });
    return () => {
      cancelled = true;
    };
  }, [mode, periodCluster, hours, metricsKey]);

  // Instance mode — fetch instances when the cluster changes and set defaults.
  useEffect(() => {
    if (!instanceCluster || mode !== "instance") return;
    fetchClusterInstances(instanceCluster)
      .then(({ instances }) => {
        setClusterInstances(instances);
        // Default A to the writer, B to the first reader (or writer if only one).
        const writer = instances.find((i) => i.role === "writer");
        const reader = instances.find((i) => i.role !== "writer");
        setInstanceA(writer?.id || instances[0]?.id || "");
        setInstanceB(reader?.id || instances[1]?.id || instances[0]?.id || "");
      })
      .catch((e) => console.error("instances fetch failed:", e));
  }, [instanceCluster, mode]);

  // Instance load effect — fetch both instances in parallel.
  useEffect(() => {
    if (
      mode !== "instance" ||
      !instanceCluster ||
      !instanceA ||
      !instanceB ||
      instanceA === instanceB ||
      metricIds.length === 0
    )
      return;
    let cancelled = false;
    setLoadingA(true);
    setLoadingB(true);
    Promise.allSettled([
      fetchBatchTimeseries(instanceCluster, metricIds, hours, 0, instanceA),
      fetchBatchTimeseries(instanceCluster, metricIds, hours, 0, instanceB),
    ]).then(([a, b]) => {
      if (cancelled) return;
      if (a.status === "fulfilled") setSeriesA(a.value.series || {});
      if (b.status === "fulfilled") setSeriesB(b.value.series || {});
      setLoadingA(false);
      setLoadingB(false);
    });
    return () => {
      cancelled = true;
    };
  }, [mode, instanceCluster, instanceA, instanceB, hours, metricsKey]);

  const labelA =
    mode === "cluster"
      ? clusterA || "A"
      : mode === "instance"
        ? instanceA || "A"
        : "현재";
  const labelB =
    mode === "cluster"
      ? clusterB || "B"
      : mode === "instance"
        ? instanceB || "B"
        : PERIOD_SHIFT_LABEL[hours] || `−${hours}h`;

  // Recharts injects series colors as inline svg attrs, so light-mode
  // contrast comes from swapping the hex itself rather than CSS overrides.
  const chart = useChartColors();
  const colorA = chart.amber;
  const colorB = chart.sky;

  return (
    <PageBody>
      <PageHeader
        eyebrow="모니터"
        title="Compare"
        description="멀티 클러스터 비교 또는 같은 클러스터의 시간대별 변화를 사이드바이사이드로 확인."
        actions={
          <div className="flex items-center gap-1">
            {RANGE_OPTIONS.map((r) => (
              <button
                key={r.hours}
                onClick={() => setHours(r.hours)}
                className={`text-xs px-3 py-1.5 border transition-colors ${
                  hours === r.hours
                    ? "border-amber-500/60 text-amber-300 bg-amber-500/10"
                    : "border-zinc-700 text-zinc-400 hover:text-zinc-100"
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        }
      />

      <div className="flex items-center gap-2 mb-4">
        <button
          onClick={() => setMode("cluster")}
          className={`text-xs px-3 py-1.5 border transition-colors ${
            mode === "cluster"
              ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
              : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Cluster vs Cluster
        </button>
        <button
          onClick={() => setMode("period")}
          className={`text-xs px-3 py-1.5 border transition-colors ${
            mode === "period"
              ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
              : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Period vs Period
        </button>
        <button
          onClick={() => setMode("instance")}
          className={`text-xs px-3 py-1.5 border transition-colors ${
            mode === "instance"
              ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
              : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
          }`}
        >
          인스턴스
        </button>
      </div>

      {mode === "cluster" ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-6">
          <ClusterPicker
            label="A"
            color={colorA}
            value={clusterA}
            clusters={clusters}
            onChange={(v) => {
              setClusterA(v);
              // When A changes, ensure B is still same-family; if not, reset B.
              const newFam = engineFamily(
                clusters.find((c) => c.cluster_id === v)?.engine,
              );
              const bRow = clusters.find((c) => c.cluster_id === clusterB);
              if (bRow && engineFamily(bRow.engine) !== newFam) {
                const sameFam = clusters.filter(
                  (c) => engineFamily(c.engine) === newFam,
                );
                setClusterB(
                  sameFam.find((c) => c.cluster_id !== v)?.cluster_id || v,
                );
              }
            }}
          />
          <ClusterPicker
            label="B"
            color={colorB}
            value={clusterB}
            clusters={candidatesB}
            onChange={setClusterB}
          />
        </div>
      ) : mode === "instance" ? (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-6">
          <ClusterPicker
            label="cluster"
            color={colorA}
            value={instanceCluster}
            clusters={clusters}
            onChange={setInstanceCluster}
          />
          <InstancePicker
            label="A"
            color={colorA}
            value={instanceA}
            instances={clusterInstances}
            onChange={setInstanceA}
          />
          <InstancePicker
            label="B"
            color={colorB}
            value={instanceB}
            instances={clusterInstances}
            onChange={setInstanceB}
          />
        </div>
      ) : (
        <div className="mb-6 flex flex-col md:flex-row md:items-center gap-3">
          <ClusterPicker
            label="cluster"
            color={colorA}
            value={periodCluster}
            clusters={clusters}
            onChange={setPeriodCluster}
          />
          <div className="text-xs text-zinc-500 leading-tight">
            <span className="text-amber-300">current</span> = last {hours}h ·{" "}
            <span className="text-sky-300">
              {PERIOD_SHIFT_LABEL[hours] || `previous ${hours}h`}
            </span>{" "}
            = same length, shifted back
          </div>
        </div>
      )}

      {mode === "instance" && instanceCluster && clusterInstances.length < 2 ? (
        <div className="border border-amber-500/30 bg-amber-500/5 text-amber-300 text-sm px-4 py-3 mb-4">
          이 클러스터는 인스턴스가 1대뿐이라 비교할 수 없습니다. 인스턴스가 여러
          대인 클러스터를 선택하세요.
        </div>
      ) : mode === "instance" &&
        instanceA &&
        instanceB &&
        instanceA === instanceB ? (
        <div className="border border-amber-500/30 bg-amber-500/5 text-amber-300 text-sm px-4 py-3 mb-4">
          비교하려면 서로 다른 인스턴스를 선택하세요.
        </div>
      ) : null}

      {crossFamilyMismatch && (
        <div className="border border-amber-500/30 bg-amber-500/5 text-amber-300 text-sm px-4 py-3 flex items-center justify-between gap-3 mb-4">
          <span>
            A({FAMILY_META[famA].label})와 B({FAMILY_META[famB].label})가 다른
            엔진 패밀리입니다 — 같은 패밀리끼리만 비교할 수 있습니다. B를
            초기화합니다.
          </span>
          <button
            onClick={() => {
              const sameFam = clusters.filter(
                (c) => engineFamily(c.engine) === famA,
              );
              setClusterB(
                sameFam.find((c) => c.cluster_id !== clusterA)?.cluster_id ||
                  clusterA,
              );
            }}
            className="text-xs px-3 py-1.5 border border-amber-500/40 hover:bg-amber-500/15 transition-colors flex-shrink-0"
          >
            B 초기화
          </button>
        </div>
      )}

      {clustersError ? (
        <div className="border border-rose-500/40 bg-rose-500/10 text-rose-300 text-sm px-4 py-3 flex items-center justify-between gap-3">
          <span>
            클러스터 목록을 불러오지 못했습니다 — 네트워크/세션 문제일 수
            있습니다.
          </span>
          <button
            onClick={loadClusters}
            className="text-xs px-3 py-1.5 border border-rose-500/40 hover:bg-rose-500/15 transition-colors flex-shrink-0"
          >
            다시 시도
          </button>
        </div>
      ) : clusters.length < 2 && mode === "cluster" ? (
        <div className="border border-amber-500/30 bg-amber-500/5 text-amber-300 text-sm px-4 py-3">
          Cluster vs Cluster 비교에는 등록된 클러스터가 2개 이상 필요합니다.{" "}
          <a href="/clusters" className="underline">
            Clusters
          </a>{" "}
          페이지에서 추가 등록하거나 샘플 클러스터를 생성하세요.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {visibleMetrics.map((m) => {
            const a = seriesA[m.id] || [];
            const b = seriesB[m.id] || [];
            const data = mergeForChart(a, b, labelA, labelB);
            const loading = loadingA || loadingB;
            return (
              <Expandable key={m.id} title={m.label}>
                <div className="bg-zinc-900/50 border border-zinc-800 p-3">
                  <div className="flex items-baseline justify-between mb-2 pr-8">
                    <div className="text-xs text-zinc-300">{m.label}</div>
                    {m.unit && (
                      <div className="text-[10px] text-zinc-500">{m.unit}</div>
                    )}
                  </div>
                  <div className="h-40">
                    {loading ? (
                      <div className="h-full flex items-center justify-center text-xs text-zinc-500">
                        불러오는 중…
                      </div>
                    ) : data.length === 0 ? (
                      <div className="h-full flex items-center justify-center text-xs text-zinc-600">
                        데이터 없음
                      </div>
                    ) : (
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart
                          data={data}
                          margin={{ top: 4, right: 8, bottom: 0, left: -20 }}
                        >
                          <CartesianGrid
                            strokeDasharray="3 3"
                            stroke={chart.grid}
                            vertical={false}
                          />
                          <XAxis
                            dataKey="ts"
                            stroke={chart.axis}
                            fontSize={9}
                            interval="preserveStartEnd"
                          />
                          <YAxis
                            stroke={chart.axis}
                            fontSize={9}
                            tickFormatter={(v) =>
                              m.fmt ? m.fmt(Number(v)) : String(v)
                            }
                          />
                          <Tooltip
                            contentStyle={{
                              background: chart.tooltipBg,
                              border: `1px solid ${chart.tooltipBorder}`,
                              fontSize: 11,
                            }}
                            labelStyle={{ color: chart.tooltipText }}
                            formatter={(value: unknown) => {
                              const num = Number(value);
                              if (!Number.isFinite(num))
                                return String(value ?? "—");
                              return m.fmt ? m.fmt(num) : String(num);
                            }}
                          />
                          <Legend wrapperStyle={{ fontSize: 10 }} />
                          <Line
                            type="monotone"
                            dataKey={labelA}
                            stroke={colorA}
                            strokeWidth={2}
                            dot={false}
                            isAnimationActive={false}
                          />
                          <Line
                            type="monotone"
                            dataKey={labelB}
                            stroke={colorB}
                            strokeWidth={2}
                            dot={false}
                            strokeDasharray={
                              mode === "period" ? "4 3" : undefined
                            }
                            isAnimationActive={false}
                          />
                        </LineChart>
                      </ResponsiveContainer>
                    )}
                  </div>
                </div>
              </Expandable>
            );
          })}
        </div>
      )}
    </PageBody>
  );
}

function InstancePicker({
  label,
  color,
  value,
  instances,
  onChange,
}: {
  label: string;
  color: string;
  value: string;
  instances: ClusterInstance[];
  onChange: (v: string) => void;
}) {
  return (
    <div className="flex items-center gap-2 bg-zinc-900/40 border border-zinc-800 px-3 py-2">
      <span className="w-2 h-2 rounded-full" style={{ background: color }} />
      <div className="font-mono text-[10px] tracking-wider uppercase text-zinc-500 w-16">
        {label}
      </div>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="flex-1 bg-zinc-950 text-zinc-100 border border-zinc-800 px-2 py-1 text-xs focus:outline-none focus:border-amber-500/60"
      >
        {instances.length === 0 && <option value="">(인스턴스 없음)</option>}
        {instances.map((inst) => (
          <option key={inst.id} value={inst.id}>
            {inst.id} ({inst.role})
          </option>
        ))}
      </select>
    </div>
  );
}

function ClusterPicker({
  label,
  color,
  value,
  clusters,
  onChange,
}: {
  label: string;
  color: string;
  value: string;
  clusters: ClusterRow[];
  onChange: (v: string) => void;
}) {
  const selected = clusters.find((c) => c.cluster_id === value);
  const badge = engineBadge(selected?.engine);

  // Group options by engine group so the <select> is organised. We use native
  // <optgroup> because a custom popover would be disproportionate here and the
  // existing ClusterPicker is a compact inline control.
  // Aurora PG and Aurora MySQL now appear as separate optgroups.
  const byGroup = groupByEngineGroup(clusters);
  const groupSections = ENGINE_GROUP_ORDER.map((g) => ({
    grp: g,
    meta: ENGINE_GROUP_META[g],
    items: byGroup[g],
  })).filter((s) => s.items.length > 0);

  return (
    <div className="flex items-center gap-2 bg-zinc-900/40 border border-zinc-800 px-3 py-2">
      <span className="w-2 h-2 rounded-full" style={{ background: color }} />
      <div className="font-mono text-[10px] tracking-wider uppercase text-zinc-500 w-16">
        {label}
      </div>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="flex-1 bg-zinc-950 text-zinc-100 border border-zinc-800 px-2 py-1 text-xs focus:outline-none focus:border-amber-500/60"
      >
        {clusters.length === 0 && <option value="">(no clusters)</option>}
        {groupSections.length === 1
          ? // Single group — no optgroup needed, just plain options.
            groupSections[0].items.map((c) => (
              <option key={c.cluster_id} value={c.cluster_id}>
                {displayName(c)}
              </option>
            ))
          : groupSections.map(({ grp, meta, items }) => (
              <optgroup key={grp} label={meta.label}>
                {items.map((c) => (
                  <option key={c.cluster_id} value={c.cluster_id}>
                    {displayName(c)}
                  </option>
                ))}
              </optgroup>
            ))}
      </select>
      {selected && (
        <span
          className={`px-1.5 py-0.5 border text-[9px] font-mono uppercase tracking-wider ${badge.classes}`}
        >
          {badge.short}
        </span>
      )}
    </div>
  );
}
