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

export async function fetchDashboard(clusterId: string) {
  const res = await fetch(await api(`/api/dashboard/${enc(clusterId)}`));
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
    await api(`/api/dashboard/${enc(clusterId)}/vacuum-stats`),
  );
  if (!res.ok) throw new Error(`Vacuum stats fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchIndexRecommendations(
  clusterId: string,
  minSeqRatio = 0.5,
) {
  const res = await fetch(
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
  const res = await fetch(
    await api(`/api/dashboard/${enc(clusterId)}/long-running`),
  );
  if (!res.ok) throw new Error(`Long running fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchBlockingLocks(clusterId: string) {
  const res = await fetch(
    await api(`/api/dashboard/${enc(clusterId)}/blocking-locks`),
  );
  if (!res.ok) throw new Error(`Locks fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchClusterSettings(clusterId: string) {
  const res = await fetch(
    await api(`/api/dashboard/${enc(clusterId)}/settings`),
  );
  if (!res.ok) throw new Error(`Settings fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchSchemaChanges(clusterId: string, days = 7) {
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
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
  count: number;
  entries: { ts: string; message: string }[];
  error?: string;
}

export async function fetchLogInsights(
  clusterId: string,
  category: LogCategory = "all",
  hours = 1,
): Promise<LogInsightsResponse> {
  const res = await fetch(
    await api(
      `/api/dashboard/${enc(clusterId)}/log-insights?category=${enc(
        category,
      )}&hours=${hours}`,
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
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
}

export async function fetchTopology(
  clusterId: string,
): Promise<TopologyResponse> {
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(await api(`/api/multi-cluster/overview`));
  if (!res.ok)
    throw new Error(`Multi-cluster overview fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchTableSizes(clusterId: string) {
  const res = await fetch(
    await api(`/api/dashboard/${enc(clusterId)}/table-sizes`),
  );
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
  const res = await fetch(await api(`/api/alert-rules`), {
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
  const res = await fetch(await api(`/api/alert-rules/${id}`), {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(updates),
  });
  if (!res.ok) throw new Error(`Update alert rule failed: ${res.status}`);
  return res.json();
}

export async function deleteAlertRule(id: number) {
  const res = await fetch(await api(`/api/alert-rules/${id}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`Delete alert rule failed: ${res.status}`);
  return res.json();
}

export async function fetchAlertSubscriptions() {
  const res = await fetch(await api(`/api/alert-subscriptions`));
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
  const res = await fetch(await api(`/api/alert-subscriptions`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ protocol, endpoint }),
  });
  if (!res.ok) throw new Error(`Create subscription failed: ${res.status}`);
  return res.json();
}

export async function deleteAlertSubscription(subArn: string) {
  const res = await fetch(
    await api(`/api/alert-subscriptions?sub_arn=${enc(subArn)}`),
    { method: "DELETE", headers: { ...(await authHeaders()) } },
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
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Register failed: ${res.status}`);
  return res.json();
}

export async function generateSampleCluster(): Promise<{
  status: string;
  cluster_id: string;
  is_demo: boolean;
  rows: Record<string, number>;
}> {
  const res = await fetch(await api(`/api/clusters/sample`), {
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
  const res = await fetch(await api(`/api/clusters/${enc(clusterId)}`), {
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
  const res = await fetch(await api(`/api/clusters/discover`), {
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
  const res = await fetch(await api(`/api/clusters/bulk-register`), {
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
  const res = await fetch(await api(`/api/explain`), {
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
  downtime_text: string;
  downtime_seconds: number;
  risk: "low" | "medium" | "moderate" | string;
}

export interface UpgradeImpactResponse {
  cluster_id: string;
  current_version: string;
  target_version: string;
  storage_gb: number;
  methods: UpgradeImpactMethod[];
  recommendation: string;
}

export interface UpgradePlanStep {
  step: number;
  action: string;
  details: string;
}

export interface UpgradePlanResponse {
  cluster_id: string;
  target_version: string;
  method: string;
  steps: UpgradePlanStep[];
  rollback_plan: string;
  estimated_total_minutes: number;
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
}

export interface ScalingResponse {
  cluster_id: string;
  is_serverless_v2: boolean;
  current: { min_acu: number; max_acu: number };
  proposed: { min_acu: number; max_acu: number };
  cost_assumption: string;
  cost_impact: {
    current_monthly_usd: number;
    proposed_monthly_usd: number;
    delta_monthly_usd: number;
    change_pct: number;
  };
  warnings: string[];
  notes: string;
}

export interface DdlImpactResponse {
  cluster_id: string;
  ddl: string;
  table: string;
  table_info: { rows: number; size_mb: number };
  estimated_seconds: number;
  online_ddl_possible: boolean;
  lock_type: string;
  disk_space_needed_mb: number;
  recommendation: string;
}

async function simPost<T>(path: string, body: object): Promise<T> {
  const res = await fetch(await api(path), {
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
  const res = await fetch(await api(`/api/simulation/parameter-catalog`));
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
): Promise<ScalingResponse> {
  return simPost(`/api/simulation/scaling`, {
    cluster_id: clusterId,
    new_min_acu: newMinAcu,
    new_max_acu: newMaxAcu,
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
