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

// Mutation calls send the Cognito ID token so the Lambda handlers can apply
// RBAC (admin vs dbops-viewer). Read-only fetches don't need auth — the
// dashboard handler doesn't gate them today. Get a *valid* token (auto-
// refresh if expiring) so a long-idle tab can still mutate.
async function authHeaders(): Promise<Record<string, string>> {
  // Defer to auth.ts which already manages refresh. Imported lazily to keep
  // this module tree-shake-friendly for SSR / non-window contexts.
  try {
    const { getValidIdToken } = await import("./auth");
    const tok = await getValidIdToken();
    return tok ? { Authorization: `Bearer ${tok}` } : {};
  } catch {
    return {};
  }
}

// Every REST route is now behind the API Gateway Cognito JWT authorizer, so
// EVERY call (reads included) must carry a token — not just mutations. This
// wrapper injects the auth header centrally; explicit per-call headers win.
export async function authedFetch(
  url: string,
  init: RequestInit = {},
): Promise<Response> {
  const auth = await authHeaders();
  return fetch(url, { ...init, headers: { ...auth, ...(init.headers || {}) } });
}

export async function fetchDashboard(clusterId: string) {
  const res = await authedFetch(await api(`/api/dashboard/${enc(clusterId)}`));
  if (!res.ok) throw new Error(`Dashboard fetch failed: ${res.status}`);
  return res.json();
}

// Time window selection used by Dashboard / Compare. Either a relative window
// ending at "now" (preset) or an absolute [from, to] window (custom picker).
// Absolute mode lets URLs encode an exact window for sharing/incident review.
export type TimeRange =
  | { kind: "preset"; hours: number }
  | { kind: "custom"; from: string; to: string };

export function timeRangeToQs(range: TimeRange): string {
  if (range.kind === "custom") {
    return `from=${enc(range.from)}&to=${enc(range.to)}`;
  }
  return `hours=${range.hours}`;
}

/** Translate either a TimeRange or a legacy `hours: number` to query params.
 *  Callers that still pass `hours` (1/6/24) keep working unchanged. */
function rangeQs(rangeOrHours: TimeRange | number): string {
  if (typeof rangeOrHours === "number") {
    return `hours=${rangeOrHours}`;
  }
  return timeRangeToQs(rangeOrHours);
}

export async function fetchTimeseries(
  clusterId: string,
  metric: string,
  rangeOrHours: TimeRange | number = 1,
) {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(clusterId)}/timeseries?metric=${enc(
        metric,
      )}&${rangeQs(rangeOrHours)}`,
    ),
  );
  if (!res.ok) throw new Error(`Timeseries fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchBatchTimeseries(
  clusterId: string,
  metrics: string[],
  rangeOrHours: TimeRange | number = 1,
  offsetHours = 0,
) {
  const csv = metrics.map(enc).join(",");
  // offset_hours is only meaningful in preset mode (compare page period-over-
  // period). Custom mode encodes the absolute window via from/to instead.
  const offsetQs =
    typeof rangeOrHours === "number" && offsetHours > 0
      ? `&offset_hours=${offsetHours}`
      : "";
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(
        clusterId,
      )}/batch-timeseries?metrics=${csv}&${rangeQs(rangeOrHours)}${offsetQs}`,
    ),
  );
  if (!res.ok) throw new Error(`Batch timeseries fetch failed: ${res.status}`);
  return res.json() as Promise<{
    cluster_id: string;
    hours: number;
    offset_hours?: number;
    from?: string | null;
    to?: string | null;
    series: Record<
      string,
      Array<{ ts: string; value: number | string; dimensions?: string }>
    >;
  }>;
}

export async function fetchWaitEvents(clusterId: string, hours = 1) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/wait-events?hours=${hours}`),
  );
  if (!res.ok) throw new Error(`Wait events fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchSlowQueries(
  clusterId: string,
  hours = 1,
  thresholdMs = 100,
) {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(
        clusterId,
      )}/slow-queries?hours=${hours}&threshold_ms=${thresholdMs}`,
    ),
  );
  if (!res.ok) throw new Error(`Slow queries fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchQueryDetail(clusterId: string, queryHash: string) {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(clusterId)}/query-detail?query_hash=${enc(
        queryHash,
      )}`,
    ),
  );
  if (!res.ok) throw new Error(`Query detail fetch failed: ${res.status}`);
  return res.json();
}

export interface TableIndex {
  index_name: string;
  definition: string;
  bytes: number;
  idx_scan: number;
  idx_tup_read: number;
  is_unique: boolean;
  is_primary: boolean;
  is_valid: boolean;
}

export interface HealthFinding {
  id: number;
  check_type: string;
  severity: "critical" | "warning" | "info";
  subject: string;
  value_str: string;
  threshold_str: string;
  recommendation: string;
  details: string | Record<string, unknown> | null;
  snapshot_time: string;
}

export interface InstalledExtension {
  extname: string;
  extversion: string;
  updated_at: string;
}

export interface RecommendedExtension {
  extname: string;
  severity: "critical" | "warning" | "info";
  why: string;
  installed: boolean;
}

export async function fetchExtensions(clusterId: string): Promise<{
  cluster_id: string;
  installed: InstalledExtension[];
  recommended: RecommendedExtension[];
}> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/extensions`),
  );
  if (!res.ok) throw new Error(`Extensions fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchHealthFindings(clusterId: string): Promise<{
  cluster_id: string;
  snapshot_time: string | null;
  counts: { critical: number; warning: number; info: number };
  findings: HealthFinding[];
}> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/health-findings`),
  );
  if (!res.ok) throw new Error(`Health findings fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchTableIndexes(
  clusterId: string,
  schema: string,
  table: string,
): Promise<{ schema: string; table: string; indexes: TableIndex[] }> {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(clusterId)}/table-indexes?schema=${enc(
        schema,
      )}&table=${enc(table)}`,
    ),
  );
  if (!res.ok) throw new Error(`Indexes fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchVacuumStats(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/vacuum-stats`),
  );
  if (!res.ok) throw new Error(`Vacuum stats fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchIndexRecommendations(
  clusterId: string,
  minSeqRatio = 0.5,
) {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(
        clusterId,
      )}/index-recommendations?min_seq_ratio=${minSeqRatio}`,
    ),
  );
  if (!res.ok) throw new Error(`Index recs fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchLongRunningQueries(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/long-running`),
  );
  if (!res.ok) throw new Error(`Long running fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchBlockingLocks(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/blocking-locks`),
  );
  if (!res.ok) throw new Error(`Locks fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchClusterSettings(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/settings`),
  );
  if (!res.ok) throw new Error(`Settings fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchSchemaChanges(clusterId: string, days = 7) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/schema-changes?days=${days}`),
  );
  if (!res.ok) throw new Error(`Schema changes fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchAnomalies(
  clusterId: string,
  hours = 4,
  threshold = 2.5,
) {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(
        clusterId,
      )}/anomalies?hours=${hours}&threshold=${threshold}`,
    ),
  );
  if (!res.ok) throw new Error(`Anomalies fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchAuditLog(
  clusterId: string,
  days = 7,
  actionType?: string,
) {
  const at = actionType ? `&action_type=${enc(actionType)}` : "";
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/audit-log?days=${days}${at}`),
  );
  if (!res.ok) throw new Error(`Audit log fetch failed: ${res.status}`);
  return res.json();
}

export type LogCategory = "all" | "slow" | "vacuum" | "error" | "connection";

export interface LogInsightsResponse {
  cluster_id: string;
  category: LogCategory;
  hours: number;
  log_group: string;
  compiled_query?: string;
  keywords?: string;
  count: number;
  entries: { ts: string; message: string }[];
  error?: string;
}

export async function fetchLogInsights(
  clusterId: string,
  category: LogCategory = "all",
  hours = 1,
  keywords: string = "",
): Promise<LogInsightsResponse> {
  const params = new URLSearchParams();
  params.set("category", category);
  params.set("hours", String(hours));
  if (keywords.trim()) params.set("q", keywords.trim());
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(clusterId)}/log-insights?${params.toString()}`,
    ),
  );
  if (!res.ok) throw new Error(`Log insights fetch failed: ${res.status}`);
  return res.json();
}

export type CapacityMetric = "storage_bytes" | "connections" | "aas";

export interface CapacityForecastResponse {
  cluster_id: string;
  metric: CapacityMetric;
  label?: string;
  current: number;
  slope_per_day: number;
  limit: number;
  days_until_limit: number | null;
  forecast: "growing" | "stable" | "shrinking";
  samples: number;
  days_lookback: number;
  projections: { d30: number; d60: number; d90: number };
  error?: string;
}

export type RedundantIndexKind = "prefix" | "duplicate" | "unused";

export interface RedundantIndexCandidate {
  schema: string;
  table: string;
  index_name: string;
  kind: RedundantIndexKind;
  bytes: number;
  idx_scan: number;
  is_unique: boolean;
  columns: string;
  definition: string;
  covered_by: string | null;
}

export interface RedundantIndexesResponse {
  cluster_id: string;
  engine: string;
  indexes_scanned?: number;
  candidates_count?: number;
  total_bytes_reclaimable?: number;
  candidates: RedundantIndexCandidate[];
  error?: string;
  info?: string;
  message?: string;
}

export async function fetchRedundantIndexes(
  clusterId: string,
): Promise<RedundantIndexesResponse> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/redundant-indexes`),
  );
  if (!res.ok) throw new Error(`Redundant indexes fetch failed: ${res.status}`);
  return res.json();
}

export interface SchemaGraphTable {
  table_name: string;
  row_count: number;
  size_bytes: number;
  fk_in: number;
  fk_out: number;
  isolated: boolean;
}

export interface SchemaGraphEdge {
  source_table: string;
  target_table: string;
  target_schema: string;
  constraint_name: string;
  definition: string;
  source_columns: string;
  target_columns: string;
}

export interface SchemaGraphResponse {
  cluster_id: string;
  engine?: string;
  schema?: string;
  tables_count?: number;
  edges_count?: number;
  isolated_count?: number;
  tables: SchemaGraphTable[];
  edges: SchemaGraphEdge[];
  error?: string;
  info?: string;
  message?: string;
}

export async function fetchSchemaGraph(
  clusterId: string,
  schema = "public",
): Promise<SchemaGraphResponse> {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(clusterId)}/schema-graph?schema=${enc(schema)}`,
    ),
  );
  if (!res.ok) throw new Error(`Schema graph fetch failed: ${res.status}`);
  return res.json();
}

export interface SloDayBucket {
  day: string;
  availability_pct: number;
  avg_latency_ms: number;
  availability_ok: boolean;
  latency_ok: boolean;
  no_data: boolean;
}

export interface SloResponse {
  cluster_id: string;
  window_days: number;
  expected_minutes: number;
  availability: {
    target_pct: number;
    actual_pct: number;
    ok_minutes: number;
    budget_consumed_pct: number | null;
    allowed_downtime_minutes: number;
    actual_downtime_minutes: number;
  };
  latency: {
    target_ms: number;
    compliance_pct: number | null;
    overall_avg_ms: number;
    budget_consumed_pct: number | null;
    samples_minutes: number;
  };
  timeline: SloDayBucket[];
}

export async function fetchSlo(
  clusterId: string,
  days: number,
  availabilityTargetPct: number,
  latencyTargetMs: number,
): Promise<SloResponse> {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(
        clusterId,
      )}/slo?days=${days}&availability_target=${availabilityTargetPct}&latency_target_ms=${latencyTargetMs}`,
    ),
  );
  if (!res.ok) throw new Error(`SLO fetch failed: ${res.status}`);
  return res.json();
}

export interface TopologyMember {
  instance_id: string;
  is_writer: boolean;
  promotion_tier: number | null;
  parameter_group_status: string;
  instance_class: string;
  instance_status: string;
  engine_version: string;
  availability_zone: string;
  replica_lag_ms: number | null;
}

export interface TopologyResponse {
  cluster_id: string;
  engine?: string;
  engine_version?: string;
  endpoint?: string;
  reader_endpoint?: string;
  multi_az?: boolean;
  status?: string;
  members_count?: number;
  members: TopologyMember[];
  error?: string;
  // true when `error` is an informational notice (demo/unregistered cluster).
  info?: boolean;
}

export async function fetchTopology(
  clusterId: string,
): Promise<TopologyResponse> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/topology`),
  );
  if (!res.ok) throw new Error(`Topology fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchCapacityForecast(
  clusterId: string,
  metric: CapacityMetric = "storage_bytes",
  daysLookback = 30,
): Promise<CapacityForecastResponse> {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(clusterId)}/capacity-forecast?metric=${enc(
        metric,
      )}&days_lookback=${daysLookback}`,
    ),
  );
  if (!res.ok) throw new Error(`Capacity forecast fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchMultiClusterOverview() {
  const res = await authedFetch(await api(`/api/multi-cluster/overview`));
  if (!res.ok)
    throw new Error(`Multi-cluster overview fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchTableSizes(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/table-sizes`),
  );
  if (!res.ok) throw new Error(`Table sizes fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchAlertRules(clusterId?: string) {
  const url = clusterId
    ? await api(`/api/alert-rules?cluster_id=${enc(clusterId)}`)
    : await api(`/api/alert-rules`);
  const res = await authedFetch(url);
  if (!res.ok) throw new Error(`Alert rules fetch failed: ${res.status}`);
  return res.json();
}

export type AlertComparison = ">" | ">=" | "<" | "<=" | "==" | "!=";
export type AlertAgg = "max" | "min" | "avg" | "last";

export interface AlertOperand {
  metric_type: string;
  comparison: AlertComparison;
  threshold: number;
  window_minutes?: number;
  agg?: AlertAgg;
}

export interface AlertConditions {
  logic: "and" | "or";
  operands: AlertOperand[];
}

export async function createAlertRule(rule: {
  cluster_id: string;
  name?: string;
  metric_type?: string;
  comparison?: AlertComparison;
  threshold?: number;
  enabled?: boolean;
  conditions?: AlertConditions;
}) {
  const res = await authedFetch(await api(`/api/alert-rules`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(rule),
  });
  if (!res.ok) throw new Error(`Create alert rule failed: ${res.status}`);
  return res.json();
}

export async function updateAlertRule(
  id: number,
  updates: Partial<{
    enabled: boolean;
    threshold: number;
    name: string;
  }>,
) {
  const res = await authedFetch(await api(`/api/alert-rules/${id}`), {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(updates),
  });
  if (!res.ok) throw new Error(`Update alert rule failed: ${res.status}`);
  return res.json();
}

export async function deleteAlertRule(id: number) {
  const res = await authedFetch(await api(`/api/alert-rules/${id}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`Delete alert rule failed: ${res.status}`);
  return res.json();
}

export interface AlertImpact {
  rule: {
    id: number;
    cluster_id: string;
    name: string;
    last_triggered_at: string | null;
  };
  window: { center: string; minutes: number } | null;
  info?: string;
  top_slow_queries: Array<{
    query_hash: string;
    query_excerpt: string;
    calls: number;
    total_ms: number;
    mean_ms: number;
  }>;
  concurrent_events: Array<{
    event_time: string;
    event_type: string;
    severity: string;
    message: string;
  }>;
  concurrent_alerts: Array<{
    event_time: string;
    rule_id: string;
    message: string;
  }>;
}

export async function fetchAlertImpact(id: number): Promise<AlertImpact> {
  const res = await authedFetch(await api(`/api/alert-rules/${id}/impact`));
  if (!res.ok) throw new Error(`Alert impact fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchAlertSubscriptions() {
  const res = await authedFetch(await api(`/api/alert-subscriptions`));
  if (!res.ok)
    throw new Error(`Alert subscriptions fetch failed: ${res.status}`);
  return res.json() as Promise<{
    topic_arn: string;
    subscriptions: {
      subscription_arn: string;
      protocol: string;
      endpoint: string;
    }[];
  }>;
}

export async function createAlertSubscription(
  protocol: string,
  endpoint: string,
) {
  const res = await authedFetch(await api(`/api/alert-subscriptions`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ protocol, endpoint }),
  });
  if (!res.ok) throw new Error(`Create subscription failed: ${res.status}`);
  return res.json();
}

export async function deleteAlertSubscription(subArn: string) {
  const res = await authedFetch(
    await api(`/api/alert-subscriptions?sub_arn=${enc(subArn)}`),
    { method: "DELETE", headers: { ...(await authHeaders()) } },
  );
  if (!res.ok) throw new Error(`Delete subscription failed: ${res.status}`);
  return res.json();
}

export async function fetchClusters() {
  const res = await authedFetch(await api(`/api/clusters`));
  if (!res.ok) throw new Error(`Clusters fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchCost(days = 30) {
  const res = await authedFetch(await api(`/api/cost?days=${days}`));
  if (!res.ok) throw new Error(`Cost fetch failed: ${res.status}`);
  return res.json() as Promise<{
    env: string;
    range_days: number;
    start: string;
    end: string;
    total: number;
    total_tagged?: number;
    currency: string;
    daily: { date: string; amount: number }[];
    by_usage_type: { usage_type: string; amount: number; quantity: number }[];
    no_data_reason?: string | null;
    tag_warning?: string | null;
    discovered_services?: string[];
  }>;
}

export async function fetchModels() {
  const res = await authedFetch(await api(`/api/models`));
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
  const res = await authedFetch(await api(`/api/clusters`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Register failed: ${res.status}`);
  return res.json();
}

export interface TestConnectionResult {
  ok: boolean;
  steps: Array<{
    name: "assume_role" | "describe_cluster" | "master_user_secret";
    status: "ok" | "failed" | "skipped" | "warning";
    error?: string;
    note?: string;
    engine?: string;
    version?: string;
    endpoint?: string;
    secret_arn?: string;
  }>;
}

export async function testClusterConnection(input: {
  cluster_id: string;
  region: string;
  spoke_role_arn?: string;
}): Promise<TestConnectionResult> {
  const res = await authedFetch(await api(`/api/clusters/test-connection`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(`Test connection failed: ${res.status}`);
  return res.json();
}

export async function generateSampleCluster(): Promise<{
  status: string;
  cluster_id: string;
  is_demo: boolean;
  rows: Record<string, number>;
}> {
  const res = await authedFetch(await api(`/api/clusters/sample`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(
      `Sample generation failed (${res.status}): ${txt.slice(0, 200)}`,
    );
  }
  return res.json();
}

export async function deleteCluster(
  clusterId: string,
): Promise<{ status: string; was_demo: boolean }> {
  const res = await authedFetch(await api(`/api/clusters/${enc(clusterId)}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`Delete failed: ${res.status}`);
  return res.json();
}

// --- Bulk cluster discovery & registration (P2.5) ---
export interface DiscoveredCluster {
  cluster_id: string;
  cluster_arn: string;
  engine: string;
  engine_version: string;
  endpoint: string;
  status: string;
  db_name: string;
  secret_arn: string;
  master_secret_arn?: string;
  // "convention" = dbops/<cluster_id>/readonly found (dedicated user, recommended)
  // "master_fallback" = no convention secret, using master user (works but blast radius)
  // "missing" = neither found, cluster cannot be registered without manual setup
  secret_source?: "convention" | "master_fallback" | "missing";
  region: string;
  account_id: string;
  already_registered: boolean;
}

export interface DiscoverResult {
  clusters: DiscoveredCluster[];
  errors: Record<string, string>;
  scanned_regions: string[];
}

export async function discoverClusters(input: {
  regions: string[];
  role_arn?: string;
  account_id?: string;
}): Promise<DiscoverResult> {
  const res = await authedFetch(await api(`/api/clusters/discover`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`Discover failed (${res.status}): ${txt.slice(0, 200)}`);
  }
  return res.json();
}

export interface BulkRegisterResult {
  registered: { cluster_id: string; connection_status?: string }[];
  skipped: { cluster_id: string; reason: string }[];
  failed: { cluster_id: string; error: string }[];
  counts: { registered: number; skipped: number; failed: number };
}

export async function bulkRegisterClusters(
  clusters: Array<
    Partial<DiscoveredCluster> & {
      account_id: string;
      spoke_role_arn?: string;
      force?: boolean;
    }
  >,
): Promise<BulkRegisterResult> {
  const res = await authedFetch(await api(`/api/clusters/bulk-register`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ clusters }),
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(
      `Bulk register failed (${res.status}): ${txt.slice(0, 200)}`,
    );
  }
  return res.json();
}

// --- EXPLAIN plan visualizer ---
//
// PG `EXPLAIN (FORMAT JSON, ANALYZE, BUFFERS)` returns a one-element array of
// {Plan, "Planning Time", "Execution Time"}. The Plan object's keys use the
// historical "Title Case With Spaces" naming, plus an optional Plans[] of
// recursive children. MySQL returns a different shape (query_block); the
// visualizer treats anything non-PG as raw for now.
export type PgPlanNode = {
  "Node Type": string;
  Plans?: PgPlanNode[];
  "Plan Rows"?: number;
  "Plan Width"?: number;
  "Actual Rows"?: number;
  "Actual Loops"?: number;
  "Actual Startup Time"?: number;
  "Actual Total Time"?: number;
  "Startup Cost"?: number;
  "Total Cost"?: number;
  "Shared Hit Blocks"?: number;
  "Shared Read Blocks"?: number;
  "Shared Dirtied Blocks"?: number;
  "Shared Written Blocks"?: number;
  "Relation Name"?: string;
  "Index Name"?: string;
  Alias?: string;
  "Join Type"?: string;
  Strategy?: string;
  "Sort Key"?: string[];
  Filter?: string;
  "Index Cond"?: string;
  "Hash Cond"?: string;
  [k: string]: unknown;
};

export type PgPlanRoot = {
  Plan: PgPlanNode;
  "Planning Time"?: number;
  "Execution Time"?: number;
  Triggers?: unknown[];
};

export interface ExplainResponse {
  engine: string;
  cluster_id: string;
  elapsed_ms: number;
  sql: string;
  explain_sql: string;
  plan: PgPlanRoot[] | Record<string, unknown> | null;
  row_count: number;
}

// Distinguish SQL errors (user typed bad SQL — show as a warning) from
// infrastructure errors (network, IAM, cluster down — show as a failure).
export class ExplainSqlError extends Error {
  readonly kind = "sql" as const;
  readonly engine?: string;
  constructor(message: string, engine?: string) {
    super(message);
    this.engine = engine;
  }
}

export async function runExplain(
  clusterId: string,
  sql: string,
): Promise<ExplainResponse> {
  const res = await authedFetch(await api(`/api/explain`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cluster_id: clusterId, sql }),
  });
  if (!res.ok) {
    let parsed: { error?: string; message?: string; engine?: string } = {};
    try {
      parsed = await res.json();
    } catch {
      // fall through to text
    }
    if (res.status === 400 && parsed.error === "sql_error") {
      throw new ExplainSqlError(parsed.message || "SQL error", parsed.engine);
    }
    const detail =
      parsed.message ||
      (await res.text().catch(() => "")) ||
      `HTTP ${res.status}`;
    throw new Error(`EXPLAIN failed: ${detail.slice(0, 300)}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Simulation MCP — REST mirror
// ---------------------------------------------------------------------------

export interface ParameterCatalogEntry {
  name: string;
  type: "static" | "dynamic" | "unknown";
  impact: string;
  restart: boolean;
}

export interface UpgradeCompatibilityResponse {
  cluster_id: string;
  engine: string;
  current_version: string;
  target_version: string;
  is_compatible: boolean;
  valid_upgrade_targets: string[];
  target_description: string;
  target_release_date: string;
}

export interface UpgradeImpactMethod {
  method: "in_place" | "blue_green" | "clone";
  estimated_minutes: number;
  // Low–high range conveying the genuine uncertainty of the estimate.
  range_low_minutes?: number;
  range_high_minutes?: number;
  downtime_text: string;
  downtime_seconds: number;
  risk: "low" | "medium" | "moderate" | string;
  // The factors that drove this method's number (object count, jump, readers…).
  basis?: string[];
}

export interface UpgradeImpactResponse {
  cluster_id: string;
  current_version: string;
  target_version: string;
  engine?: string;
  storage_gb: number;
  // Object-count-driven model fields.
  upgrade_type?: "major" | "minor";
  major_jump?: number;
  readers?: number;
  table_count?: number | null;
  object_count_basis?: string;
  confidence?: "low" | "medium" | "high";
  methods: UpgradeImpactMethod[];
  recommendation: string;
  recommendation_reason?: string;
  methodology_note?: string;
}

export interface UpgradePlanStep {
  step: number;
  action: string;
  details: string;
}

export interface UpgradePlanResponse {
  cluster_id: string;
  current_version?: string;
  target_version: string;
  engine?: string;
  upgrade_type?: "major" | "minor";
  readers?: number;
  table_count?: number | null;
  method: string;
  steps: UpgradePlanStep[];
  rollback_plan: string;
  estimated_total_minutes: number;
  estimated_range_minutes?: [number, number];
  downtime_text?: string;
  confidence?: "low" | "medium" | "high";
  object_count_basis?: string;
  methodology_note?: string;
}

export interface ParameterChangeResponse {
  cluster_id: string;
  parameter: string;
  new_value: string;
  known: boolean;
  is_dynamic: boolean;
  requires_restart: boolean;
  impact_area: string;
  recommendation: string;
  // Live-metadata fields (present when the cluster's parameter group was read).
  current_value?: string | null;
  current_value_note?: string;
  is_modifiable?: boolean;
  allowed_values?: string | null;
  data_type?: string | null;
  parameter_group?: string;
  impact_note?: string;
  source?: string;
  valid?: boolean;
  validation_reason?: string;
  data_source?: string;
}

// serverless mode carries min_acu/max_acu; provisioned mode carries
// instance_class. Both are optional on the union so the panel branches on
// `mode` and reads whichever fields are present.
export interface ScalingTier {
  min_acu?: number;
  max_acu?: number;
  instance_class?: string;
}

export interface ScalingUnitPricing {
  kind: "acu" | "instance";
  price_per_hour: number | null;
  region: string;
  io_optimized: boolean;
  source: "aws_pricing_api" | "fallback";
}

export interface ScalingResponse {
  cluster_id: string;
  mode: "serverless" | "provisioned";
  // serverless: observed average ACU drives the cost when CloudWatch has data,
  // else falls back to the min/max midpoint (acu_basis tells which).
  observed_avg_acu?: number | null;
  acu_basis?: "observed" | "midpoint";
  confidence?: "low" | "medium" | "high";
  current: ScalingTier;
  proposed: ScalingTier;
  writers: number;
  readers: number;
  cost_impact: {
    current_monthly_usd: number | null;
    proposed_monthly_usd: number | null;
    delta_monthly_usd: number | null;
    change_pct: number | null;
  };
  unit_pricing: ScalingUnitPricing;
  data_source: string;
  note: string;
}

export interface DdlImpactResponse {
  cluster_id: string;
  ddl: string;
  table: string;
  operation?: string;
  table_info: { rows: number; size_mb: number };
  estimated_seconds: number;
  estimated_range_seconds?: [number, number];
  throughput_mb_s?: number;
  online_ddl_possible: boolean;
  lock_type: string;
  disk_space_needed_mb: number;
  confidence?: "low" | "medium" | "high";
  basis?: string[];
  recommendation: string;
  note?: string;
}

async function simPost<T>(path: string, body: object): Promise<T> {
  const res = await authedFetch(await api(path), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `Simulation request failed (${res.status}): ${text.slice(0, 200)}`,
    );
  }
  return res.json();
}

export async function fetchParameterCatalog(): Promise<{
  parameters: ParameterCatalogEntry[];
}> {
  const res = await authedFetch(await api(`/api/simulation/parameter-catalog`));
  if (!res.ok) throw new Error(`Parameter catalog fetch failed: ${res.status}`);
  return res.json();
}

export function simulateUpgradeCompatibility(
  clusterId: string,
  targetVersion: string,
): Promise<UpgradeCompatibilityResponse> {
  return simPost(`/api/simulation/upgrade-compatibility`, {
    cluster_id: clusterId,
    target_version: targetVersion,
  });
}

export function simulateUpgradeImpact(
  clusterId: string,
  targetVersion: string,
): Promise<UpgradeImpactResponse> {
  return simPost(`/api/simulation/upgrade-impact`, {
    cluster_id: clusterId,
    target_version: targetVersion,
  });
}

export function simulateUpgradePlan(
  clusterId: string,
  targetVersion: string,
  method: "blue_green" | "in_place" | "clone",
): Promise<UpgradePlanResponse> {
  return simPost(`/api/simulation/upgrade-plan`, {
    cluster_id: clusterId,
    target_version: targetVersion,
    method,
  });
}

export function simulateParameterChange(
  clusterId: string,
  parameterName: string,
  newValue: string,
): Promise<ParameterChangeResponse> {
  return simPost(`/api/simulation/parameter-change`, {
    cluster_id: clusterId,
    parameter_name: parameterName,
    new_value: newValue,
  });
}

export function simulateScaling(
  clusterId: string,
  newMinAcu: number | null,
  newMaxAcu: number | null,
  newInstanceClass?: string | null,
): Promise<ScalingResponse> {
  return simPost(`/api/simulation/scaling`, {
    cluster_id: clusterId,
    new_min_acu: newMinAcu,
    new_max_acu: newMaxAcu,
    new_instance_class: newInstanceClass ?? null,
  });
}

export function simulateDdlImpact(
  clusterId: string,
  ddlSql: string,
): Promise<DdlImpactResponse> {
  return simPost(`/api/simulation/ddl-impact`, {
    cluster_id: clusterId,
    ddl_sql: ddlSql,
  });
}

// ---------------------------------------------------------------------------
// Runbooks API
// ---------------------------------------------------------------------------

export interface RunbookListItem {
  id: number;
  cluster_id: string | null;
  title: string;
  summary_md: string | null;
  tags: string[];
  source: string | null;
  source_ref: string | null;
  created_by: string | null;
  created_at: string;
}

export interface RunbookDetail extends RunbookListItem {
  body_md: string;
  updated_at: string;
}

export async function fetchRunbooks(opts?: {
  clusterId?: string;
  tag?: string;
  limit?: number;
}): Promise<{ runbooks: RunbookListItem[]; count: number }> {
  const params = new URLSearchParams();
  if (opts?.clusterId) params.set("cluster_id", opts.clusterId);
  if (opts?.tag) params.set("tag", opts.tag);
  if (opts?.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const url = `/api/runbooks${qs ? "?" + qs : ""}`;
  const res = await authedFetch(await api(url));
  if (!res.ok) throw new Error(`Runbooks fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchRunbook(id: number): Promise<RunbookDetail> {
  const res = await authedFetch(await api(`/api/runbooks/${id}`));
  if (!res.ok) throw new Error(`Runbook fetch failed: ${res.status}`);
  return res.json();
}

export async function createRunbook(input: {
  cluster_id?: string | null;
  title: string;
  summary_md?: string;
  body_md: string;
  tags?: string[];
  source?: "chat" | "anomaly" | "manual";
  source_ref?: string;
}): Promise<{ runbook: RunbookDetail }> {
  const res = await authedFetch(await api(`/api/runbooks`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(
      `Create runbook failed (${res.status}): ${detail.slice(0, 200)}`,
    );
  }
  return res.json();
}

export async function deleteRunbook(id: number): Promise<void> {
  const res = await authedFetch(await api(`/api/runbooks/${id}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`Delete runbook failed: ${res.status}`);
}

// =====  Chat sessions (cross-device conversation persistence) =====

export interface ChatSessionSummary {
  session_id: string;
  title: string;
  cluster_id: string;
  updated_at: number;
  created_at: number;
  message_count: number;
}

export interface ChatSessionDetail extends ChatSessionSummary {
  messages: Array<{
    role: string;
    content: string;
    tool_calls?: unknown[];
    ts?: number;
  }>;
}

export async function listChatSessions(): Promise<ChatSessionSummary[]> {
  const res = await authedFetch(await api(`/api/chat/sessions`), {
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`List chat sessions failed: ${res.status}`);
  const body = await res.json();
  return body.sessions || [];
}

export async function fetchChatSession(id: string): Promise<ChatSessionDetail> {
  const res = await authedFetch(
    await api(`/api/chat/sessions/${encodeURIComponent(id)}`),
    {
      headers: { ...(await authHeaders()) },
    },
  );
  if (!res.ok) throw new Error(`Fetch chat session failed: ${res.status}`);
  return res.json();
}

export async function putChatSession(
  id: string,
  payload: { title: string; cluster_id: string; messages: unknown[] },
): Promise<void> {
  const res = await authedFetch(
    await api(`/api/chat/sessions/${encodeURIComponent(id)}`),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...(await authHeaders()) },
      body: JSON.stringify(payload),
    },
  );
  if (!res.ok) throw new Error(`Put chat session failed: ${res.status}`);
}

export async function deleteChatSession(id: string): Promise<void> {
  const res = await authedFetch(
    await api(`/api/chat/sessions/${encodeURIComponent(id)}`),
    {
      method: "DELETE",
      headers: { ...(await authHeaders()) },
    },
  );
  if (!res.ok) throw new Error(`Delete chat session failed: ${res.status}`);
}

// =====  Saved queries (Query Lab scratchpad) =====

export interface SavedQuerySummary {
  id: number;
  cluster_id: string | null;
  title: string;
  description: string;
  tags: string[];
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface SavedQueryDetail extends SavedQuerySummary {
  sql_text: string;
}

export async function listSavedQueries(opts?: {
  cluster_id?: string;
  tag?: string;
  limit?: number;
}): Promise<SavedQuerySummary[]> {
  const params = new URLSearchParams();
  if (opts?.cluster_id) params.set("cluster_id", opts.cluster_id);
  if (opts?.tag) params.set("tag", opts.tag);
  if (opts?.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const url = await api(`/api/saved-queries${qs ? `?${qs}` : ""}`);
  const res = await authedFetch(url);
  if (!res.ok) throw new Error(`List saved queries failed: ${res.status}`);
  const body = await res.json();
  return body.queries || [];
}

export async function fetchSavedQuery(id: number): Promise<SavedQueryDetail> {
  const res = await authedFetch(await api(`/api/saved-queries/${id}`));
  if (!res.ok) throw new Error(`Fetch saved query failed: ${res.status}`);
  return res.json();
}

export async function createSavedQuery(input: {
  cluster_id?: string | null;
  title: string;
  description?: string;
  sql_text: string;
  tags?: string[];
}): Promise<SavedQueryDetail> {
  const res = await authedFetch(await api(`/api/saved-queries`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(
      `Create saved query failed (${res.status}): ${detail.slice(0, 200)}`,
    );
  }
  return res.json();
}

export async function deleteSavedQuery(id: number): Promise<void> {
  const res = await authedFetch(await api(`/api/saved-queries/${id}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`Delete saved query failed: ${res.status}`);
}

// =====  Self-monitoring health =====

export interface HealthResponse {
  checked_at: number;
  elapsed_ms: number;
  lambdas: {
    error?: string;
    count?: number;
    active?: number;
    items?: Array<{
      name: string;
      runtime: string;
      state: string;
      last_modified: string;
      memory_mb: number;
      timeout_s: number;
    }>;
  };
  aurora: {
    error?: string;
    cluster_id?: string;
    status?: string;
    engine?: string;
    engine_version?: string;
    endpoint?: string;
    serverless_min_acu?: number;
    serverless_max_acu?: number;
    multi_az?: boolean;
    deletion_protection?: boolean;
  };
  ddb: {
    error?: string;
    tables?: Array<{
      label: string;
      name: string;
      status?: string;
      item_count?: number;
      size_bytes?: number;
      error?: string;
    }>;
  };
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await authedFetch(await api(`/api/health`));
  if (!res.ok) throw new Error(`Health fetch failed: ${res.status}`);
  return res.json();
}

// =====  DBOps activity log =====

export interface ActivityItem {
  approval_id: string;
  created_at: string;
  resolved_at?: string;
  consumed_at?: string;
  approval_status: "pending" | "approved" | "rejected" | "consumed" | string;
  cluster_id: string;
  action_type: string;
  requested_by: string;
  approved_by?: string;
  action_details_excerpt: string;
}

export async function fetchActivity(opts?: {
  cluster_id?: string;
  actor?: string;
  action_type?: string;
  limit?: number;
}): Promise<{ items: ActivityItem[]; count: number }> {
  const params = new URLSearchParams();
  if (opts?.cluster_id) params.set("cluster_id", opts.cluster_id);
  if (opts?.actor) params.set("actor", opts.actor);
  if (opts?.action_type) params.set("action_type", opts.action_type);
  if (opts?.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  const url = await api(`/api/activity${qs ? `?${qs}` : ""}`);
  const res = await authedFetch(url);
  if (!res.ok) throw new Error(`Activity fetch failed: ${res.status}`);
  return res.json();
}

// =====  Backup inventory (snapshots + PITR window) =====

export interface BackupSnapshot {
  id: string;
  type: "manual" | "automated" | string;
  status: string;
  created: string | null;
  engine_version: string;
  allocated_storage_gb: number | null;
}

export interface BackupsResponse {
  cluster_id: string;
  engine: string;
  status: string;
  error?: string;
  // true when `error` is an informational notice (demo/unregistered cluster),
  // not a real failure — render neutral, not red.
  info?: boolean;
  backup_retention_days: number | null;
  preferred_backup_window: string | null;
  earliest_restorable_time: string | null;
  latest_restorable_time: string | null;
  pitr_window_hours: number | null;
  snapshot_count: number;
  manual_snapshot_count: number;
  snapshots: BackupSnapshot[];
  checked_at: number;
}

export async function fetchBackups(
  clusterId: string,
): Promise<BackupsResponse> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/backups`),
  );
  if (!res.ok) throw new Error(`Backups fetch failed: ${res.status}`);
  return res.json();
}

export interface CreateSnapshotResponse {
  ok: boolean;
  cluster_id: string;
  snapshot_id: string;
  status: string;
  created_by: string;
  message: string;
}

// Manual snapshot creation — admin-gated write. snapshotId optional;
// backend auto-generates a valid id when omitted.
export async function createSnapshot(
  clusterId: string,
  snapshotId?: string,
): Promise<CreateSnapshotResponse> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/snapshot`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await authHeaders()) },
      body: JSON.stringify(snapshotId ? { snapshot_id: snapshotId } : {}),
    },
  );
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(
      `Create snapshot failed (${res.status}): ${detail.slice(0, 200)}`,
    );
  }
  return res.json();
}

export interface RestoreResponse {
  ok: boolean;
  cluster_id: string;
  new_cluster_id: string;
  mode: string;
  restore_source: string;
  registered: boolean;
  created_by: string;
  message: string;
}

export interface RestoreRequest {
  newClusterId: string;
  // type-to-confirm: the value the user re-typed. Sent verbatim; the server
  // requires it to equal newClusterId. The friction lives in the UI.
  confirm: string;
  mode: "snapshot" | "pitr";
  snapshotId?: string;
  restoreToTime?: string; // ISO 8601, for mode=pitr
  useLatest?: boolean; // mode=pitr → restore to latest restorable time
}

// Restore a snapshot or point-in-time into a NEW cluster — admin-gated +
// type-to-confirm. The restored cluster is provisioned async (writer
// instance added by the restore_finalizer once it is available).
export async function restoreCluster(
  clusterId: string,
  opts: RestoreRequest,
): Promise<RestoreResponse> {
  const body: Record<string, unknown> = {
    new_cluster_id: opts.newClusterId,
    confirm: opts.confirm,
    mode: opts.mode,
  };
  if (opts.mode === "snapshot") {
    body.snapshot_id = opts.snapshotId;
  } else if (opts.useLatest) {
    body.use_latest = true;
  } else {
    body.restore_to_time = opts.restoreToTime;
  }
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/restore`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await authHeaders()) },
      body: JSON.stringify(body),
    },
  );
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`Restore failed (${res.status}): ${detail.slice(0, 300)}`);
  }
  return res.json();
}

// =====  Workload diff (pg_stat_statements snapshot delta) =====

export interface WorkloadDiffResponse {
  cluster_id: string;
  before: string;
  after: string;
  regression_pct: number;
  match_window_min: number;
  totals: {
    before_distinct_queries: number;
    after_distinct_queries: number;
    new: number;
    disappeared: number;
    regressed: number;
    improved: number;
  };
  new: Array<{
    query_hash: string;
    query_excerpt: string;
    mean_time_ms: number;
    calls: number;
  }>;
  regressed: Array<{
    query_hash: string;
    query_excerpt: string;
    before_mean_ms: number;
    after_mean_ms: number;
    delta_pct: number;
  }>;
  improved: WorkloadDiffResponse["regressed"];
  disappeared: Array<{
    query_hash: string;
    query_excerpt: string;
    mean_time_ms: number;
  }>;
  methodology: string;
}

export async function fetchWorkloadDiff(
  clusterId: string,
  beforeIso: string,
  afterIso: string,
  opts?: { regressionPct?: number; matchWindowMin?: number },
): Promise<WorkloadDiffResponse> {
  const params = new URLSearchParams();
  params.set("before", beforeIso);
  params.set("after", afterIso);
  if (opts?.regressionPct != null)
    params.set("regression_pct", String(opts.regressionPct));
  if (opts?.matchWindowMin != null)
    params.set("match_window_min", String(opts.matchWindowMin));
  const url = await api(
    `/api/dashboard/${enc(clusterId)}/workload-diff?${params.toString()}`,
  );
  const res = await authedFetch(url);
  if (!res.ok) throw new Error(`Workload diff fetch failed: ${res.status}`);
  return res.json();
}

// =====  Unified incident timeline =====

export type TimelineCategory =
  | "alert"
  | "rds_event"
  | "proactive"
  | "ack"
  | "schema_change"
  | "audit";

export interface TimelineItem {
  ts: string;
  category: TimelineCategory | string;
  severity: string;
  title: string;
  detail: string;
  source: string;
  source_id: string;
}

export interface TimelineResponse {
  cluster_id: string;
  hours: number;
  categories: string[];
  count: number;
  items: TimelineItem[];
}

export async function fetchTimeline(
  clusterId: string,
  opts?: { hours?: number; categories?: string[] },
): Promise<TimelineResponse> {
  const params = new URLSearchParams();
  if (opts?.hours) params.set("hours", String(opts.hours));
  if (opts?.categories?.length)
    params.set("categories", opts.categories.join(","));
  const qs = params.toString();
  const url = await api(
    `/api/dashboard/${enc(clusterId)}/timeline${qs ? `?${qs}` : ""}`,
  );
  const res = await authedFetch(url);
  if (!res.ok) throw new Error(`Timeline fetch failed: ${res.status}`);
  return res.json();
}

// =====  Agent memory (read + prune AgentCore Memory records) =====

export type MemoryKind = "preferences" | "facts";

export interface MemoryRecord {
  id: string;
  content: string;
  created_at: string;
  updated_at: string;
}

export async function listMemoryRecords(
  kind: MemoryKind,
): Promise<{ namespace: string; kind: MemoryKind; records: MemoryRecord[] }> {
  const res = await authedFetch(
    await api(`/api/memory?kind=${encodeURIComponent(kind)}`),
    {
      headers: { ...(await authHeaders()) },
    },
  );
  if (!res.ok) throw new Error(`List memory failed: ${res.status}`);
  return res.json();
}

export async function deleteMemoryRecord(
  id: string,
  kind: MemoryKind,
): Promise<void> {
  const res = await authedFetch(
    await api(`/api/memory/${encodeURIComponent(id)}?kind=${kind}`),
    {
      method: "DELETE",
      headers: { ...(await authHeaders()) },
    },
  );
  if (!res.ok) throw new Error(`Delete memory failed: ${res.status}`);
}
