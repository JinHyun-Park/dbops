"use client";

import { useState, useEffect } from "react";
import { ClusterOverview } from "@/components/dashboard/cluster-overview";
import { AasChart } from "@/components/dashboard/aas-chart";
import { MetricCard } from "@/components/design-system/metric-card";
import { fetchClusters, fetchDashboard } from "@/lib/api-client";

export default function DashboardPage() {
  const [clusters, setClusters] = useState<any[]>([]);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [dashboardData, setDashboardData] = useState<any>(null);

  useEffect(() => {
    fetchClusters().then(setClusters).catch(console.error);
  }, []);

  useEffect(() => {
    if (!selectedCluster) return;
    const load = () =>
      fetchDashboard(selectedCluster)
        .then(setDashboardData)
        .catch(console.error);
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, [selectedCluster]);

  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 p-6">
      <h1 className="text-2xl font-bold mb-6">Dashboard</h1>

      <ClusterOverview
        clusters={clusters}
        selectedId={selectedCluster}
        onSelect={setSelectedCluster}
      />

      {dashboardData && (
        <div className="mt-6 space-y-6">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {dashboardData.metrics?.map((m: any) => (
              <MetricCard
                key={m.metric_type}
                label={m.metric_type}
                value={Number(m.avg_val).toFixed(2)}
              />
            ))}
          </div>
          <AasChart
            data={
              dashboardData.metrics?.filter(
                (m: any) => m.metric_type === "aas",
              ) || []
            }
          />
        </div>
      )}
    </div>
  );
}
