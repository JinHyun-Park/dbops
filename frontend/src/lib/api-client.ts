// Runtime config: resolved on first call by fetching /config.json from the same origin.
// During `next dev` (no /config.json), falls back to NEXT_PUBLIC_API_URL or window.location.origin.

interface RuntimeConfig {
  apiUrl: string;
  frontendUrl?: string;
  region?: string;
  cognitoDomain?: string;
}

let configPromise: Promise<RuntimeConfig> | null = null;

function loadConfig(): Promise<RuntimeConfig> {
  if (configPromise) return configPromise;
  configPromise = (async () => {
    if (typeof window === "undefined") {
      return { apiUrl: process.env.NEXT_PUBLIC_API_URL || "" };
    }
    try {
      const res = await fetch("/config.json", { cache: "no-store" });
      if (res.ok) {
        const cfg = (await res.json()) as RuntimeConfig;
        if (cfg.apiUrl) {
          // Strip trailing slash for clean URL joins
          cfg.apiUrl = cfg.apiUrl.replace(/\/+$/, "");
          return cfg;
        }
      }
    } catch {
      // ignore and fall through
    }
    const fallback =
      process.env.NEXT_PUBLIC_API_URL ||
      (typeof window !== "undefined" ? window.location.origin : "");
    return { apiUrl: fallback.replace(/\/+$/, "") };
  })();
  return configPromise;
}

async function api(path: string): Promise<string> {
  const cfg = await loadConfig();
  return `${cfg.apiUrl}${path}`;
}

export async function apiUrl(path: string): Promise<string> {
  return api(path);
}

const enc = encodeURIComponent;

export async function fetchDashboard(clusterId: string) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}`));
  if (!res.ok) throw new Error(`Dashboard fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchTimeseries(clusterId: string, metric: string, hours = 1) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/timeseries?metric=${enc(metric)}&hours=${hours}`));
  if (!res.ok) throw new Error(`Timeseries fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchBatchTimeseries(clusterId: string, metrics: string[], hours = 1) {
  const csv = metrics.map(enc).join(",");
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/batch-timeseries?metrics=${csv}&hours=${hours}`));
  if (!res.ok) throw new Error(`Batch timeseries fetch failed: ${res.status}`);
  return res.json() as Promise<{
    cluster_id: string;
    hours: number;
    series: Record<string, Array<{ ts: string; value: number | string; dimensions?: string }>>;
  }>;
}

export async function fetchWaitEvents(clusterId: string, hours = 1) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/wait-events?hours=${hours}`));
  if (!res.ok) throw new Error(`Wait events fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchSlowQueries(clusterId: string, hours = 1, thresholdMs = 100) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/slow-queries?hours=${hours}&threshold_ms=${thresholdMs}`));
  if (!res.ok) throw new Error(`Slow queries fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchQueryDetail(clusterId: string, queryHash: string) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/query-detail?query_hash=${enc(queryHash)}`));
  if (!res.ok) throw new Error(`Query detail fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchVacuumStats(clusterId: string) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/vacuum-stats`));
  if (!res.ok) throw new Error(`Vacuum stats fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchIndexRecommendations(clusterId: string, minSeqRatio = 0.5) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/index-recommendations?min_seq_ratio=${minSeqRatio}`));
  if (!res.ok) throw new Error(`Index recs fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchLongRunningQueries(clusterId: string) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/long-running`));
  if (!res.ok) throw new Error(`Long running fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchBlockingLocks(clusterId: string) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/blocking-locks`));
  if (!res.ok) throw new Error(`Locks fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchClusterSettings(clusterId: string) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/settings`));
  if (!res.ok) throw new Error(`Settings fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchSchemaChanges(clusterId: string, days = 7) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/schema-changes?days=${days}`));
  if (!res.ok) throw new Error(`Schema changes fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchAnomalies(clusterId: string, hours = 4, threshold = 2.5) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/anomalies?hours=${hours}&threshold=${threshold}`));
  if (!res.ok) throw new Error(`Anomalies fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchAuditLog(clusterId: string, days = 7, actionType?: string) {
  const at = actionType ? `&action_type=${enc(actionType)}` : "";
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/audit-log?days=${days}${at}`));
  if (!res.ok) throw new Error(`Audit log fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchMultiClusterOverview() {
  const res = await fetch(await api(`/api/multi-cluster/overview`));
  if (!res.ok) throw new Error(`Multi-cluster overview fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchTableSizes(clusterId: string) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}/table-sizes`));
  if (!res.ok) throw new Error(`Table sizes fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchAlertRules(clusterId?: string) {
  const url = clusterId
    ? await api(`/api/alert-rules?cluster_id=${enc(clusterId)}`)
    : await api(`/api/alert-rules`);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Alert rules fetch failed: ${res.status}`);
  return res.json();
}

export async function createAlertRule(rule: {
  cluster_id: string;
  name?: string;
  metric_type: string;
  comparison: ">" | ">=" | "<" | "<=" | "==" | "!=";
  threshold: number;
  enabled?: boolean;
}) {
  const res = await fetch(await api(`/api/alert-rules`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(rule),
  });
  if (!res.ok) throw new Error(`Create alert rule failed: ${res.status}`);
  return res.json();
}

export async function updateAlertRule(id: number, updates: Partial<{
  enabled: boolean;
  threshold: number;
  name: string;
}>) {
  const res = await fetch(await api(`/api/alert-rules/${id}`), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!res.ok) throw new Error(`Update alert rule failed: ${res.status}`);
  return res.json();
}

export async function deleteAlertRule(id: number) {
  const res = await fetch(await api(`/api/alert-rules/${id}`), { method: "DELETE" });
  if (!res.ok) throw new Error(`Delete alert rule failed: ${res.status}`);
  return res.json();
}

export async function fetchAlertSubscriptions() {
  const res = await fetch(await api(`/api/alert-subscriptions`));
  if (!res.ok) throw new Error(`Alert subscriptions fetch failed: ${res.status}`);
  return res.json() as Promise<{
    topic_arn: string;
    subscriptions: { subscription_arn: string; protocol: string; endpoint: string }[];
  }>;
}

export async function createAlertSubscription(protocol: string, endpoint: string) {
  const res = await fetch(await api(`/api/alert-subscriptions`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ protocol, endpoint }),
  });
  if (!res.ok) throw new Error(`Create subscription failed: ${res.status}`);
  return res.json();
}

export async function deleteAlertSubscription(subArn: string) {
  const res = await fetch(
    await api(`/api/alert-subscriptions?sub_arn=${enc(subArn)}`),
    { method: "DELETE" },
  );
  if (!res.ok) throw new Error(`Delete subscription failed: ${res.status}`);
  return res.json();
}

export async function fetchClusters() {
  const res = await fetch(await api(`/api/clusters`));
  if (!res.ok) throw new Error(`Clusters fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchCost(days = 30) {
  const res = await fetch(await api(`/api/cost?days=${days}`));
  if (!res.ok) throw new Error(`Cost fetch failed: ${res.status}`);
  return res.json() as Promise<{
    env: string;
    range_days: number;
    start: string;
    end: string;
    total: number;
    currency: string;
    daily: { date: string; amount: number }[];
    by_usage_type: { usage_type: string; amount: number; quantity: number }[];
    no_data_reason?: string | null;
  }>;
}

export async function fetchModels() {
  const res = await fetch(await api(`/api/models`));
  if (!res.ok) throw new Error(`Models fetch failed: ${res.status}`);
  return res.json() as Promise<{
    default: string;
    region: string;
    models: { id: string; label: string; name: string; status: string }[];
    error?: string;
  }>;
}

export async function registerCluster(data: {
  cluster_id: string;
  account_id: string;
  region: string;
  engine?: string;
  spoke_role_arn?: string;
}) {
  const res = await fetch(await api(`/api/clusters`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Register failed: ${res.status}`);
  return res.json();
}
