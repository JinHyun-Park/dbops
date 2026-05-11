"use client";

import { useState, useEffect } from "react";
import { ClusterOverview } from "@/components/dashboard/cluster-overview";
import { TimeseriesChart } from "@/components/dashboard/timeseries-chart";
import { WaitEventsPanel } from "@/components/dashboard/wait-events-panel";
import { EventsPanel } from "@/components/dashboard/events-panel";
import { QueriesPanel } from "@/components/dashboard/queries-panel";
import { HealthScore } from "@/components/dashboard/health-score";
import { VacuumPanel } from "@/components/dashboard/vacuum-panel";
import { IndexRecsPanel } from "@/components/dashboard/index-recs-panel";
import { LongRunningPanel } from "@/components/dashboard/long-running-panel";
import { ConnectionBreakdown } from "@/components/dashboard/connection-breakdown";
import { LocksPanel } from "@/components/dashboard/locks-panel";
import { SettingsPanel } from "@/components/dashboard/settings-panel";
import { SchemaChangesPanel } from "@/components/dashboard/schema-changes-panel";
import { AnomaliesPanel } from "@/components/dashboard/anomalies-panel";
import { AuditLogPanel } from "@/components/dashboard/audit-log-panel";
import { TableSizesPanel } from "@/components/dashboard/table-sizes-panel";
import { BackupPanel } from "@/components/dashboard/backup-panel";
import { fetchClusters, fetchDashboard, fetchBatchTimeseries } from "@/lib/api-client";
import { PageHeader, PageBody } from "@/components/design-system/page-shell";

type TsPoint = { ts: string; value: number | string; dimensions?: string };

const CHART_METRICS = [
  "aas",
  "cpu",
  "connections",
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

export default function DashboardPage() {
  const [clusters, setClusters] = useState<{ cluster_id: string; engine?: string; status?: string }[]>([]);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [dashboardData, setDashboardData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hours, setHours] = useState<number>(1);
  const [tsBatch, setTsBatch] = useState<Record<string, TsPoint[]>>({});
  const [tsLoading, setTsLoading] = useState<boolean>(true);

  useEffect(() => {
    fetchClusters()
      .then((cs) => {
        setClusters(cs);
        if (cs.length === 0) return;
        const params = typeof window !== "undefined" ? new URLSearchParams(window.location.search) : null;
        const wanted = params?.get("cluster");
        const match = wanted && cs.find((c: { cluster_id: string }) => c.cluster_id === wanted);
        setSelectedCluster(match ? wanted : cs[0].cluster_id);
      })
      .catch((e) => setError(`Failed to load clusters: ${e.message}`));
  }, []);

  useEffect(() => {
    if (!selectedCluster) return;
    setTsBatch({});
    setTsLoading(true);
    let cancelled = false;
    const load = () => {
      fetchBatchTimeseries(selectedCluster, CHART_METRICS, hours)
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
  }, [selectedCluster, hours]);

  useEffect(() => {
    if (!selectedCluster) return;
    setDashboardData(null);
    setError(null);
    let cancelled = false;
    const load = () => {
      fetchDashboard(selectedCluster)
        .then((d) => !cancelled && setDashboardData(d))
        .catch((e) => !cancelled && setError(`Failed to load dashboard: ${e.message}`));
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
        eyebrow="monitor"
        title="Dashboard"
        description="단일 클러스터 deep dive — 시계열, wait events, locks, vacuum, schema changes 등 17개 패널."
        actions={
          <div className="flex items-center gap-1">
            <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">range</span>
            {RANGES.map((r) => (
              <button
                key={r.hours}
                onClick={() => setHours(r.hours)}
                className={`text-xs px-3 py-1.5 transition-colors ${
                  hours === r.hours
                    ? "bg-amber-500 text-zinc-950"
                    : "border border-zinc-700 text-zinc-400 hover:text-zinc-100"
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        }
      />

      <ClusterOverview
        clusters={clusters}
        selectedId={selectedCluster}
        onSelect={setSelectedCluster}
      />

      {error && (
        <div className="mt-6 bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300 text-sm">
          {error}
        </div>
      )}

      {selectedCluster && !dashboardData && !error && (
        <div className="mt-6 text-zinc-500">Loading metrics for {selectedCluster}...</div>
      )}

      {selectedCluster && dashboardData && (
        <div className="mt-6 space-y-6">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className="lg:col-span-2 bg-zinc-800 border border-zinc-700 rounded-lg p-4">
              <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">Cluster Info</div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                <div>
                  <div className="text-zinc-500 text-xs mb-1">Engine</div>
                  <div className="text-zinc-100">{dashboardData.cluster?.engine || "-"}</div>
                </div>
                <div>
                  <div className="text-zinc-500 text-xs mb-1">Version</div>
                  <div className="text-zinc-100">{dashboardData.cluster?.engine_version || "-"}</div>
                </div>
                <div>
                  <div className="text-zinc-500 text-xs mb-1">Status</div>
                  <div className="text-emerald-400">{dashboardData.cluster?.status || "-"}</div>
                </div>
                <div>
                  <div className="text-zinc-500 text-xs mb-1">Storage</div>
                  <div className="text-zinc-100">{dashboardData.cluster?.storage_size_gb ?? "-"} GB</div>
                </div>
              </div>
            </div>
            <HealthScore clusterId={selectedCluster} />
          </div>

          <BackupPanel cluster={dashboardData.cluster} />

          <TimeseriesChart
            clusterId={selectedCluster}
            metric="aas"
            title="Active Sessions (AAS) by Wait Event"
            hours={hours}
            type="stacked"
            externalPoints={tsBatch.aas || []}
            externalLoading={tsLoading}
          />

          <ConnectionBreakdown clusterId={selectedCluster} hours={hours} />

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
              metric="connections"
              title="Active Connections"
              hours={hours}
              color="#f472b6"
              type="area"
              formatValue={(v) => v.toFixed(0)}
              externalPoints={tsBatch.connections || []}
              externalLoading={tsLoading}
            />
            <TimeseriesChart
              clusterId={selectedCluster}
              metric="xact_commit"
              title="Transactions / sec"
              hours={hours}
              color="#fbbf24"
              type="line"
              formatValue={(v) => v.toFixed(1)}
              externalPoints={tsBatch.xact_commit || []}
              externalLoading={tsLoading}
            />
            <TimeseriesChart
              clusterId={selectedCluster}
              metric="tup_returned"
              title="Tuples Returned / sec"
              hours={hours}
              color="#a78bfa"
              type="line"
              formatValue={(v) => v.toFixed(0)}
              externalPoints={tsBatch.tup_returned || []}
              externalLoading={tsLoading}
            />
            <TimeseriesChart
              clusterId={selectedCluster}
              metric="storage_bytes"
              title="Storage Used"
              hours={hours}
              color="#22d3ee"
              type="area"
              formatValue={(v) => (v / 1024 / 1024 / 1024).toFixed(2) + " GB"}
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

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <WaitEventsPanel clusterId={selectedCluster} hours={hours} />
            <AnomaliesPanel clusterId={selectedCluster} />
            <EventsPanel events={dashboardData.events || []} />
          </div>

          <LocksPanel clusterId={selectedCluster} />

          <LongRunningPanel clusterId={selectedCluster} />

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <VacuumPanel clusterId={selectedCluster} />
            <IndexRecsPanel clusterId={selectedCluster} />
          </div>

          <TableSizesPanel clusterId={selectedCluster} />

          <SchemaChangesPanel clusterId={selectedCluster} />

          <QueriesPanel
            clusterId={selectedCluster}
            topQueries={dashboardData.top_queries || []}
          />

          <SettingsPanel clusterId={selectedCluster} />

          <AuditLogPanel clusterId={selectedCluster} />
        </div>
      )}
    </PageBody>
  );
}
