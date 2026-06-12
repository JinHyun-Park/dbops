"use client";

import { useState, useEffect } from "react";
import { useChartColors } from "@/lib/use-chart-colors";
import { ClusterOverview } from "@/components/dashboard/cluster-overview";
import { TimeseriesChart } from "@/components/dashboard/timeseries-chart";
import { WaitEventsPanel } from "@/components/dashboard/wait-events-panel";
import { EventsPanel } from "@/components/dashboard/events-panel";
import { QueriesPanel } from "@/components/dashboard/queries-panel";
import { HealthScore } from "@/components/dashboard/health-score";
import { VacuumPanel } from "@/components/dashboard/vacuum-panel";
import { MaintenanceHealthPanel } from "@/components/dashboard/maintenance-health-panel";
import { ExtensionsCard } from "@/components/dashboard/extensions-card";
import { IndexRecsPanel } from "@/components/dashboard/index-recs-panel";
import { RedundantIndexesPanel } from "@/components/dashboard/redundant-indexes-panel";
import { ReplicationTopologyPanel } from "@/components/dashboard/replication-topology-panel";
import { LongRunningPanel } from "@/components/dashboard/long-running-panel";
import { ConnectionBreakdown } from "@/components/dashboard/connection-breakdown";
import { LocksPanel } from "@/components/dashboard/locks-panel";
import { SettingsPanel } from "@/components/dashboard/settings-panel";
import { SchemaChangesPanel } from "@/components/dashboard/schema-changes-panel";
import { AnomaliesPanel } from "@/components/dashboard/anomalies-panel";
import { IncidentSummary } from "@/components/dashboard/incident-summary";
import { AuditLogPanel } from "@/components/dashboard/audit-log-panel";
import { ChangeImpactPanel } from "@/components/dashboard/change-impact-panel";
import { LogInsightsPanel } from "@/components/dashboard/log-insights-panel";
import { TableSizesPanel } from "@/components/dashboard/table-sizes-panel";
import { BackupPanel } from "@/components/dashboard/backup-panel";
import { EngineConfigPanel } from "@/components/dashboard/engine-config-panel";
import { CapacityForecastPanel } from "@/components/dashboard/capacity-forecast-panel";
import { DataApiBanner } from "@/components/dashboard/data-api-banner";
import {
  fetchClusters,
  fetchDashboard,
  fetchBatchTimeseries,
} from "@/lib/api-client";
import { PageHeader, PageBody } from "@/components/design-system/page-shell";
import {
  getSelectedCluster,
  setSelectedCluster as persistCluster,
  onClusterChange,
} from "@/lib/selected-cluster";
import {
  engineBadge,
  engineFamily,
  isPostgres,
  isMysql,
  eolFor,
  EOL_STATUS_CLASSES,
  eolHint,
} from "@/lib/engine";
import { DynamodbOverviewPanel } from "@/components/dashboard/dynamodb-overview-panel";
import { DocdbOverviewPanel } from "@/components/dashboard/docdb-overview-panel";

type TsPoint = { ts: string; value: number | string; dimensions?: string };

const CHART_METRICS = [
  "aas",
  "cpu",
  // Canonical total-connections metric (CloudWatch DatabaseConnections),
  // collected for every cluster — unlike the PI-only "connections".
  "db_connections",
  "read_iops",
  "write_iops",
  "xact_commit",
  "tup_returned",
  "storage_bytes",
  "replica_lag_ms",
  "deadlocks",
];

interface Event {
  ts: string;
  event_type: string;
  severity: string;
  message: string;
}

interface DashboardData {
  cluster?: {
    engine?: string;
    engine_version?: string;
    status?: string;
    storage_size_gb?: number | string;
    instance_class?: string;
    http_endpoint_enabled?: boolean | null;
    backup_retention_days?: number | string | null;
    earliest_restorable_time?: string | null;
    latest_restorable_time?: string | null;
    preferred_backup_window?: string | null;
    preferred_maintenance_window?: string | null;
    multi_az?: boolean | null;
    deletion_protection?: boolean | null;
  };
  top_queries?: {
    query_hash: string;
    query_text: string;
    calls: number | string;
    total_time_ms: number | string;
    mean_time_ms: number | string;
  }[];
  events?: Event[];
}

const RANGES = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
];

type TimeRange =
  | { kind: "preset"; hours: number }
  | { kind: "custom"; from: string; to: string };

const DEFAULT_RANGE: TimeRange = { kind: "preset", hours: 1 };

// Pretty short label for the current range (used on the Custom button).
function rangeLabel(r: TimeRange): string {
  if (r.kind === "preset") return `${r.hours}h`;
  const f = new Date(r.from);
  const t = new Date(r.to);
  const fmt = (d: Date) =>
    `${String(d.getMonth() + 1).padStart(2, "0")}/${String(
      d.getDate(),
    ).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(
      d.getMinutes(),
    ).padStart(2, "0")}`;
  return `${fmt(f)} → ${fmt(t)}`;
}

// Approximate number of hours covered by a TimeRange — used for legacy panels
// that still take `hours: number` (TimeseriesChart, ConnectionBreakdown).
// For custom ranges, hours = ceil((to - from) in hours).
function rangeToHours(r: TimeRange): number {
  if (r.kind === "preset") return r.hours;
  const ms = new Date(r.to).getTime() - new Date(r.from).getTime();
  return Math.max(1, Math.ceil(ms / 3_600_000));
}

// HTML datetime-local strings (e.g. "2026-05-18T14:00") to ISO timestamptz
// strings the API expects. We treat the picker input as local time and let
// the browser do the timezone offset.
function localToIso(local: string): string {
  return new Date(local).toISOString();
}

function isoToLocal(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

// Saved views: persisted in localStorage so a DBA can return to a specific
// cluster + range combo (e.g. "prod-write incident review", "weekend etl
// window") with one click. We keep this client-side for v1; a DDB-backed
// share-across-team sync can land in a follow-up without breaking this
// shape (the URL bar is already a one-off share path via from/to params).
interface SavedView {
  id: string;
  name: string;
  cluster_id: string;
  range: TimeRange;
  saved_at: number;
}

const VIEWS_STORAGE_KEY = "dbops_dashboard_saved_views_v1";
const VIEWS_LIMIT = 20;

function loadViews(): SavedView[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(VIEWS_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as SavedView[]) : [];
  } catch {
    return [];
  }
}

function persistViews(next: SavedView[]): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(
      VIEWS_STORAGE_KEY,
      JSON.stringify(next.slice(0, VIEWS_LIMIT)),
    );
  } catch {
    // Quota — drop oldest and retry. Saved views are nice-to-have, not
    // load-bearing, so silent failure is acceptable.
    try {
      localStorage.setItem(VIEWS_STORAGE_KEY, JSON.stringify(next.slice(0, 5)));
    } catch {
      // give up
    }
  }
}

export default function DashboardPage() {
  const [clusters, setClusters] = useState<
    {
      cluster_id: string;
      engine?: string;
      status?: string;
      is_demo?: boolean;
    }[]
  >([]);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [dashboardData, setDashboardData] = useState<DashboardData | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [range, setRange] = useState<TimeRange>(DEFAULT_RANGE);
  const chartColors = useChartColors();
  const [customOpen, setCustomOpen] = useState<boolean>(false);
  const [views, setViews] = useState<SavedView[]>([]);
  const [viewsOpen, setViewsOpen] = useState<boolean>(false);
  const [tsBatch, setTsBatch] = useState<Record<string, TsPoint[]>>({});
  const [tsLoading, setTsLoading] = useState<boolean>(true);

  // Legacy panels still take `hours: number`; derive it once per render.
  const hours = rangeToHours(range);

  // Load persisted views on mount. We also write back through every mutation
  // path below so the list is always in sync — there's no separate "save"
  // step on top of the per-action persist.
  useEffect(() => {
    setViews(loadViews());
  }, []);

  const saveCurrentView = (name: string) => {
    if (!name.trim() || !selectedCluster) return;
    const entry: SavedView = {
      id: `view-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      name: name.trim().slice(0, 60),
      cluster_id: selectedCluster,
      range,
      saved_at: Date.now(),
    };
    const next = [entry, ...views].slice(0, VIEWS_LIMIT);
    setViews(next);
    persistViews(next);
  };

  const applyView = (v: SavedView) => {
    setSelectedCluster(v.cluster_id);
    setRange(v.range);
    setViewsOpen(false);
  };

  const removeView = (id: string) => {
    const next = views.filter((v) => v.id !== id);
    setViews(next);
    persistViews(next);
  };

  useEffect(() => {
    fetchClusters()
      .then((cs) => {
        setClusters(cs);
        if (cs.length === 0) return;
        const params =
          typeof window !== "undefined"
            ? new URLSearchParams(window.location.search)
            : null;
        // Prefer the URL ?cluster=, then the globally-selected cluster (shared
        // store / localStorage), then the first cluster — so the dashboard
        // honors a switch made via ⌘K / another page.
        const wanted = params?.get("cluster") || getSelectedCluster();
        const match =
          wanted &&
          cs.find((c: { cluster_id: string }) => c.cluster_id === wanted);
        setSelectedCluster(match ? wanted : cs[0].cluster_id);

        // Initialise time range from URL — `?from=ISO&to=ISO` for absolute,
        // `?range=24h` for preset. This makes the URL bar a shareable bookmark
        // of a specific window (incident review handoffs etc.).
        if (params) {
          const fromQs = params.get("from");
          const toQs = params.get("to");
          if (fromQs && toQs && !Number.isNaN(new Date(fromQs).getTime())) {
            setRange({ kind: "custom", from: fromQs, to: toQs });
          } else {
            const r = params.get("range");
            const m = r && /^(\d+)h$/.exec(r);
            if (m) setRange({ kind: "preset", hours: parseInt(m[1], 10) });
          }
        }
      })
      .catch((e) => setError(`Failed to load clusters: ${e.message}`));
  }, []);

  // Mirror the active range back into the URL so reload + share both round-
  // trip to the same window. Cluster selection is preserved separately.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (selectedCluster) params.set("cluster", selectedCluster);
    params.delete("from");
    params.delete("to");
    params.delete("range");
    if (range.kind === "custom") {
      params.set("from", range.from);
      params.set("to", range.to);
    } else {
      params.set("range", `${range.hours}h`);
    }
    const next = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState(null, "", next);
    // Mirror the choice into the shared store so the header chip + every other
    // page follow this cluster (persists across navigation).
    if (selectedCluster) persistCluster(selectedCluster);
  }, [selectedCluster, range]);

  // Live-sync: when the cluster is switched elsewhere (⌘K palette, header chip,
  // browser back/forward), update the dashboard without a remount.
  useEffect(() => {
    return onClusterChange(() => {
      const cur = getSelectedCluster();
      if (cur) setSelectedCluster(cur);
    });
  }, []);

  useEffect(() => {
    if (!selectedCluster) return;
    setTsBatch({});
    setTsLoading(true);
    let cancelled = false;
    const load = () => {
      fetchBatchTimeseries(selectedCluster, CHART_METRICS, range)
        .then((d) => {
          if (cancelled) return;
          setTsBatch(d.series || {});
          setTsLoading(false);
        })
        .catch(() => {
          if (cancelled) return;
          setTsLoading(false);
        });
    };
    load();
    const iv = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCluster, JSON.stringify(range)]);

  useEffect(() => {
    if (!selectedCluster) return;
    setDashboardData(null);
    setError(null);
    let cancelled = false;
    const load = () => {
      fetchDashboard(selectedCluster)
        .then((d) => !cancelled && setDashboardData(d))
        .catch(
          (e) =>
            !cancelled && setError(`Failed to load dashboard: ${e.message}`),
        );
    };
    load();
    const interval = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [selectedCluster]);

  return (
    <PageBody>
      <PageHeader
        eyebrow="모니터"
        title="Dashboard"
        description="단일 클러스터 deep dive — 시계열, wait events, locks, vacuum, schema changes 등 17개 패널."
        actions={
          <div className="flex items-center gap-1 relative">
            <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
              range
            </span>
            {RANGES.map((r) => {
              const isActive =
                range.kind === "preset" && range.hours === r.hours;
              return (
                <button
                  key={r.hours}
                  onClick={() => {
                    setRange({ kind: "preset", hours: r.hours });
                    setCustomOpen(false);
                  }}
                  className={`text-xs px-3 py-1.5 transition-colors ${
                    isActive
                      ? "bg-amber-500 text-zinc-950"
                      : "border border-zinc-700 text-zinc-400 hover:text-zinc-100"
                  }`}
                >
                  {r.label}
                </button>
              );
            })}
            <button
              onClick={() => setCustomOpen((v) => !v)}
              className={`text-xs px-3 py-1.5 transition-colors ${
                range.kind === "custom"
                  ? "bg-amber-500 text-zinc-950"
                  : "border border-zinc-700 text-zinc-400 hover:text-zinc-100"
              }`}
              title={
                range.kind === "custom"
                  ? rangeLabel(range)
                  : "임의 시간 범위 지정"
              }
            >
              {range.kind === "custom" ? rangeLabel(range) : "custom"}
            </button>
            {customOpen && (
              <CustomRangePopover
                initial={range}
                onCancel={() => setCustomOpen(false)}
                onApply={(r) => {
                  setRange(r);
                  setCustomOpen(false);
                }}
              />
            )}
            <button
              onClick={() => {
                setViewsOpen((v) => !v);
                setCustomOpen(false);
              }}
              className={`ml-2 text-xs px-3 py-1.5 transition-colors ${
                viewsOpen
                  ? "bg-amber-500 text-zinc-950"
                  : "border border-zinc-700 text-zinc-400 hover:text-zinc-100"
              }`}
              title="자주 보는 클러스터 + range 조합을 핀으로 저장"
            >
              ★ Views
              {views.length > 0 && (
                <span className="ml-1 text-zinc-500">{views.length}</span>
              )}
            </button>
            {viewsOpen && (
              <SavedViewsPopover
                views={views}
                canSave={!!selectedCluster}
                currentSummary={
                  selectedCluster
                    ? `${selectedCluster} · ${rangeLabel(range)}`
                    : ""
                }
                onSave={saveCurrentView}
                onApply={applyView}
                onRemove={removeView}
                onClose={() => setViewsOpen(false)}
              />
            )}
          </div>
        }
      />

      <ClusterOverview
        clusters={clusters}
        selectedId={selectedCluster}
        onSelect={setSelectedCluster}
      />

      {/* Incident-first: when the selected cluster isn't healthy, lead with
          WHY + the RCA action before the metric panels below.
          RCA / incident diagnosis is Aurora-oriented — skip for non-relational. */}
      {selectedCluster &&
        engineFamily(
          clusters.find((c) => c.cluster_id === selectedCluster)?.engine,
        ) === "relational" && <IncidentSummary clusterId={selectedCluster} />}

      {selectedCluster &&
        clusters.find((c) => c.cluster_id === selectedCluster)?.is_demo && (
          <div className="mt-4 flex items-center gap-3 px-4 py-2.5 border border-purple-500/40 bg-purple-500/10 text-purple-200 text-xs">
            <span className="px-1.5 py-0.5 text-[10px] font-mono tracking-wider uppercase bg-purple-500/25 border border-purple-500/40">
              demo
            </span>
            <span>
              합성 데이터로 채워진 데모 클러스터입니다. 실제 Aurora가 아니라
              평가용 24시간 시드 데이터를 보고 있습니다 — Clusters 페이지에서
              언제든 삭제 가능합니다.
            </span>
          </div>
        )}

      {error && (
        <div className="mt-6 bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300 text-sm">
          {error}
        </div>
      )}

      {selectedCluster && !dashboardData && !error && (
        <div className="mt-6 text-zinc-500">
          {selectedCluster} 메트릭 불러오는 중…
        </div>
      )}

      {selectedCluster && dashboardData && (
        <div className="mt-6 space-y-6">
          {(() => {
            const eng = dashboardData.cluster?.engine || "";
            const ver = dashboardData.cluster?.engine_version || "";
            const fam = engineFamily(eng);
            const badge = engineBadge(eng);
            const eol = eolFor(eng, ver);
            // "클러스터 정보" (instances / storage / Multi-AZ) and HealthScore
            // (CPU / AAS / Connections gauge) are Aurora-relational concepts.
            // For DynamoDB / DocumentDB the family panel below carries the
            // relevant overview — render nothing here to avoid misleading "-"s.
            if (fam !== "relational") return null;
            return (
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <div className="lg:col-span-2 bg-zinc-800 border border-zinc-700 rounded-lg p-4">
                  <div className="flex items-center justify-between mb-3 gap-3">
                    <div className="text-sm text-zinc-200 font-medium">
                      클러스터 정보
                    </div>
                    <div className="flex items-center gap-2">
                      <span
                        className={`inline-flex items-center gap-1.5 px-2.5 py-1 border text-[11px] font-mono uppercase tracking-wider ${badge.classes}`}
                        title={`엔진: ${eng || "unknown"}`}
                      >
                        <span
                          className={`w-1.5 h-1.5 rounded-full ${badge.accent}`}
                        />
                        {badge.label}
                        {ver && (
                          <span className="text-zinc-300/80 normal-case font-normal">
                            {ver}
                          </span>
                        )}
                      </span>
                      {eol && (
                        <span
                          className={`px-2 py-1 border border-zinc-700 text-[10px] font-mono uppercase tracking-wider ${
                            EOL_STATUS_CLASSES[eol.status]
                          }`}
                          title={eolHint(eol)}
                        >
                          {eol.status === "expired"
                            ? `EOL · ${Math.abs(eol.days_remaining)}d past`
                            : `EOL ${eol.eol} · ${eol.days_remaining}d`}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 text-sm">
                    <div>
                      <div className="text-zinc-500 text-xs mb-1">상태</div>
                      <div className="text-emerald-400">
                        {dashboardData.cluster?.status || "-"}
                      </div>
                    </div>
                    <div>
                      <div className="text-zinc-500 text-xs mb-1">인스턴스</div>
                      <div className="text-zinc-100 font-mono text-xs">
                        {dashboardData.cluster?.instance_class || "-"}
                      </div>
                    </div>
                    <div>
                      <div className="text-zinc-500 text-xs mb-1">스토리지</div>
                      <div className="text-zinc-100">
                        {dashboardData.cluster?.storage_size_gb ?? "-"} GB
                      </div>
                    </div>
                    <div>
                      <div className="text-zinc-500 text-xs mb-1">Multi-AZ</div>
                      <div className="text-zinc-100">
                        {dashboardData.cluster?.multi_az ? "yes" : "no"}
                      </div>
                    </div>
                  </div>
                </div>
                <HealthScore clusterId={selectedCluster} engine={eng} />
              </div>
            );
          })()}

          {/* Engine-family panel gating — compute once from cluster metadata */}
          {(() => {
            const fam = engineFamily(dashboardData.cluster?.engine);
            return (
              <>
                {/* ── Relational (Aurora MySQL / PostgreSQL) panels ── */}
                {fam === "relational" && (
                  <>
                    {/* false일 때만 — NULL(미수집)·true는 숨김. 라이브 SQL 패널들이
                        영원히 "수집 대기"로 보이는 이유를 여기서 먼저 설명한다. */}
                    {dashboardData.cluster?.http_endpoint_enabled === false &&
                      selectedCluster && (
                        <DataApiBanner clusterId={selectedCluster} />
                      )}

                    <MaintenanceHealthPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />

                    {(dashboardData.cluster?.engine || "").includes(
                      "postgresql",
                    ) && (
                      <ExtensionsCard
                        clusterId={selectedCluster}
                        engine={dashboardData.cluster?.engine}
                      />
                    )}

                    <BackupPanel clusterId={selectedCluster} />

                    <ReplicationTopologyPanel clusterId={selectedCluster} />

                    <CapacityForecastPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />

                    <TimeseriesChart
                      clusterId={selectedCluster}
                      metric="aas"
                      title="Active Sessions (AAS) by Wait Event"
                      hours={hours}
                      type="stacked"
                      externalPoints={tsBatch.aas || []}
                      externalLoading={tsLoading}
                    />

                    <ConnectionBreakdown
                      clusterId={selectedCluster}
                      hours={hours}
                    />

                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                      <TimeseriesChart
                        clusterId={selectedCluster}
                        metric="cpu"
                        title="CPU Utilization"
                        hours={hours}
                        color="#34d399"
                        type="area"
                        unit="%"
                        formatValue={(v) => v.toFixed(1)}
                        externalPoints={tsBatch.cpu || []}
                        externalLoading={tsLoading}
                      />
                      <TimeseriesChart
                        clusterId={selectedCluster}
                        metric="read_iops"
                        title="Read IOPS"
                        hours={hours}
                        color="#60a5fa"
                        type="line"
                        formatValue={(v) => v.toFixed(0)}
                        externalPoints={tsBatch.read_iops || []}
                        externalLoading={tsLoading}
                      />
                      <TimeseriesChart
                        clusterId={selectedCluster}
                        metric="write_iops"
                        title="Write IOPS"
                        hours={hours}
                        color="#f472b6"
                        type="line"
                        formatValue={(v) => v.toFixed(0)}
                        externalPoints={tsBatch.write_iops || []}
                        externalLoading={tsLoading}
                      />
                      <TimeseriesChart
                        clusterId={selectedCluster}
                        metric="db_connections"
                        title="Active Connections"
                        hours={hours}
                        color="#f472b6"
                        type="area"
                        formatValue={(v) => v.toFixed(0)}
                        externalPoints={tsBatch.db_connections || []}
                        externalLoading={tsLoading}
                      />
                      {isPostgres(dashboardData.cluster?.engine) && (
                        <>
                          <TimeseriesChart
                            clusterId={selectedCluster}
                            metric="xact_commit"
                            title="Transactions / sec (PG)"
                            hours={hours}
                            color={chartColors.amber}
                            type="line"
                            formatValue={(v) => v.toFixed(1)}
                            externalPoints={tsBatch.xact_commit || []}
                            externalLoading={tsLoading}
                          />
                          <TimeseriesChart
                            clusterId={selectedCluster}
                            metric="tup_returned"
                            title="Tuples Returned / sec (PG)"
                            hours={hours}
                            color="#a78bfa"
                            type="line"
                            formatValue={(v) => v.toFixed(0)}
                            externalPoints={tsBatch.tup_returned || []}
                            externalLoading={tsLoading}
                          />
                        </>
                      )}
                      <TimeseriesChart
                        clusterId={selectedCluster}
                        metric="storage_bytes"
                        title="Storage Used"
                        hours={hours}
                        color="#22d3ee"
                        type="area"
                        formatValue={(v) =>
                          (v / 1024 / 1024 / 1024).toFixed(2) + " GB"
                        }
                        externalPoints={tsBatch.storage_bytes || []}
                        externalLoading={tsLoading}
                      />
                      <TimeseriesChart
                        clusterId={selectedCluster}
                        metric="replica_lag_ms"
                        title="Replica Lag"
                        hours={hours}
                        color="#fb7185"
                        type="line"
                        unit="ms"
                        formatValue={(v) => v.toFixed(0)}
                        externalPoints={tsBatch.replica_lag_ms || []}
                        externalLoading={tsLoading}
                      />
                      <TimeseriesChart
                        clusterId={selectedCluster}
                        metric="deadlocks"
                        title="Deadlocks"
                        hours={hours}
                        color="#ef4444"
                        type="line"
                        formatValue={(v) => v.toFixed(0)}
                        externalPoints={tsBatch.deadlocks || []}
                        externalLoading={tsLoading}
                      />
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                      <WaitEventsPanel
                        clusterId={selectedCluster}
                        hours={hours}
                      />
                      <AnomaliesPanel clusterId={selectedCluster} />
                      <EventsPanel
                        events={dashboardData.events || []}
                        clusterId={selectedCluster}
                      />
                    </div>

                    <LocksPanel clusterId={selectedCluster} />

                    <LongRunningPanel clusterId={selectedCluster} />

                    {/* Vacuum is PG-only — MySQL InnoDB has no equivalent surface.
                        For MySQL clusters the column is collapsed and the right panel
                        takes the full width. */}
                    <div
                      className={`grid grid-cols-1 ${
                        (dashboardData.cluster?.engine || "").includes(
                          "postgresql",
                        )
                          ? "md:grid-cols-2"
                          : ""
                      } gap-4`}
                    >
                      {(dashboardData.cluster?.engine || "").includes(
                        "postgresql",
                      ) && <VacuumPanel clusterId={selectedCluster} />}
                      <IndexRecsPanel clusterId={selectedCluster} />

                      <RedundantIndexesPanel clusterId={selectedCluster} />
                    </div>

                    <TableSizesPanel clusterId={selectedCluster} />

                    <SchemaChangesPanel clusterId={selectedCluster} />

                    <QueriesPanel
                      clusterId={selectedCluster}
                      topQueries={dashboardData.top_queries || []}
                    />

                    <SettingsPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />

                    <ChangeImpactPanel clusterId={selectedCluster} />

                    <AuditLogPanel clusterId={selectedCluster} />

                    <LogInsightsPanel clusterId={selectedCluster} />
                  </>
                )}

                {/* ── DynamoDB panels ── */}
                {fam === "dynamodb" && (
                  <>
                    <HealthScore
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <MaintenanceHealthPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <DynamodbOverviewPanel
                      clusterId={selectedCluster}
                      range={range}
                    />
                    <EngineConfigPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <CapacityForecastPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <BackupPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <EventsPanel
                      events={dashboardData.events || []}
                      clusterId={selectedCluster}
                    />
                  </>
                )}

                {/* ── DocumentDB panels ── */}
                {fam === "documentdb" && (
                  <>
                    <HealthScore
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <MaintenanceHealthPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <DocdbOverviewPanel
                      clusterId={selectedCluster}
                      range={range}
                    />
                    <EngineConfigPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <CapacityForecastPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <ReplicationTopologyPanel clusterId={selectedCluster} />
                    <BackupPanel
                      clusterId={selectedCluster}
                      engine={dashboardData.cluster?.engine}
                    />
                    <EventsPanel
                      events={dashboardData.events || []}
                      clusterId={selectedCluster}
                    />
                  </>
                )}
              </>
            );
          })()}
        </div>
      )}
    </PageBody>
  );
}

/** Saved-views popover — shown when the user clicks "★ Views" in the header.
 *  Top row: name input + "save current" button (saves the active cluster +
 *  range combo). Below: scrollable list of pinned views with click-to-apply
 *  + delete (×). All persistence is localStorage-only for v1. */
function SavedViewsPopover({
  views,
  canSave,
  currentSummary,
  onSave,
  onApply,
  onRemove,
  onClose,
}: {
  views: SavedView[];
  canSave: boolean;
  currentSummary: string;
  onSave: (name: string) => void;
  onApply: (v: SavedView) => void;
  onRemove: (id: string) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const submit = () => {
    if (!name.trim()) return;
    onSave(name);
    setName("");
  };
  return (
    <div className="absolute top-full right-0 mt-2 z-30 w-96 border border-zinc-700 bg-zinc-900 shadow-2xl text-xs">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-baseline justify-between gap-3">
        <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-amber-300">
          saved views
        </div>
        <button
          onClick={onClose}
          className="text-zinc-500 hover:text-zinc-200 text-base leading-none"
          aria-label="닫기"
        >
          ×
        </button>
      </div>
      <div className="px-4 py-3 border-b border-zinc-800 space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500">
          현재 화면 저장
        </div>
        <div className="text-[11px] text-zinc-400 break-all">
          {currentSummary || "클러스터 선택 후 저장 가능"}
        </div>
        <div className="flex items-center gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
            placeholder="이름 (예: prod-write incident, weekend ETL window)"
            className="flex-1 bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 focus:outline-none focus:border-amber-500/60"
          />
          <button
            onClick={submit}
            disabled={!canSave || !name.trim()}
            className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:bg-zinc-700 disabled:text-zinc-500 transition-colors"
          >
            핀
          </button>
        </div>
      </div>
      <div className="max-h-72 overflow-y-auto">
        {views.length === 0 ? (
          <div className="px-4 py-6 text-center text-zinc-500">
            아직 저장된 view 없음 · 위 입력란에 이름을 적고 [핀]을 누르세요
          </div>
        ) : (
          <ul className="divide-y divide-zinc-800">
            {views.map((v) => (
              <li
                key={v.id}
                className="px-4 py-2 hover:bg-zinc-800/40 transition-colors flex items-center justify-between gap-2"
              >
                <button
                  onClick={() => onApply(v)}
                  className="flex-1 text-left min-w-0"
                >
                  <div className="text-zinc-200 truncate">{v.name}</div>
                  <div className="text-[10px] text-zinc-500 font-mono truncate">
                    {v.cluster_id} ·{" "}
                    {v.range.kind === "preset"
                      ? `${v.range.hours}h`
                      : `${new Date(v.range.from).toLocaleDateString()} 범위`}
                  </div>
                </button>
                <button
                  onClick={() => onRemove(v.id)}
                  className="text-zinc-500 hover:text-rose-300 text-[11px] flex-shrink-0"
                  title="이 view 삭제"
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** Inline popover for picking an absolute time range. Renders below the
 *  Custom button. Two datetime-local inputs (uses native picker), Apply +
 *  cancel. Validates: to > from, max 30 days (cache DB retention). */
function CustomRangePopover({
  initial,
  onApply,
  onCancel,
}: {
  initial: TimeRange;
  onApply: (r: TimeRange) => void;
  onCancel: () => void;
}) {
  // Seed defaults: if we came from a preset, suggest "last 6h ending now"
  // so the user only has to nudge the boundaries. If we came from a custom
  // range, keep the existing values for adjustment.
  const seed = (() => {
    if (initial.kind === "custom") {
      return { from: isoToLocal(initial.from), to: isoToLocal(initial.to) };
    }
    const now = new Date();
    const back = new Date(now.getTime() - 6 * 3_600_000);
    return {
      from: isoToLocal(back.toISOString()),
      to: isoToLocal(now.toISOString()),
    };
  })();

  const [from, setFrom] = useState(seed.from);
  const [to, setTo] = useState(seed.to);
  const [err, setErr] = useState<string | null>(null);

  const apply = () => {
    if (!from || !to) {
      setErr("시작과 종료 시각을 모두 입력하세요.");
      return;
    }
    const fromMs = new Date(from).getTime();
    const toMs = new Date(to).getTime();
    if (Number.isNaN(fromMs) || Number.isNaN(toMs)) {
      setErr("유효한 시간 형식이 아닙니다.");
      return;
    }
    if (toMs <= fromMs) {
      setErr("종료 시각은 시작 시각보다 늦어야 합니다.");
      return;
    }
    if (toMs - fromMs > 30 * 86_400_000) {
      setErr("최대 30일 범위까지 조회 가능합니다.");
      return;
    }
    onApply({
      kind: "custom",
      from: localToIso(from),
      to: localToIso(to),
    });
  };

  return (
    <div className="absolute top-full right-0 mt-2 z-30 w-80 border border-zinc-700 bg-zinc-900 shadow-2xl p-4 text-xs">
      <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-amber-300 mb-2">
        custom range
      </div>
      <div className="space-y-3">
        <div>
          <label className="block text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
            시작
          </label>
          <input
            type="datetime-local"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 px-2 py-1.5 focus:outline-none focus:border-amber-500/60"
          />
        </div>
        <div>
          <label className="block text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
            종료
          </label>
          <input
            type="datetime-local"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 px-2 py-1.5 focus:outline-none focus:border-amber-500/60"
          />
        </div>
        {err && (
          <div className="text-rose-300 bg-rose-500/10 border border-rose-500/30 px-2 py-1">
            {err}
          </div>
        )}
        <div className="flex items-center justify-between pt-1">
          <div className="text-[10px] text-zinc-500">
            URL에 from/to로 인코딩되어 공유 가능
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onCancel}
              className="text-zinc-400 hover:text-zinc-200 px-2 py-1"
            >
              취소
            </button>
            <button
              onClick={apply}
              className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400"
            >
              적용
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
