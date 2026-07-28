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
  if (!res.ok) throw new Error(`대시보드 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`시계열 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchBatchTimeseries(
  clusterId: string,
  metrics: string[],
  rangeOrHours: TimeRange | number = 1,
  offsetHours = 0,
  instance?: string,
) {
  const csv = metrics.map(enc).join(",");
  // offset_hours is only meaningful in preset mode (compare page period-over-
  // period). Custom mode encodes the absolute window via from/to instead.
  const offsetQs =
    typeof rangeOrHours === "number" && offsetHours > 0
      ? `&offset_hours=${offsetHours}`
      : "";
  const instanceQs = instance ? `&instance=${enc(instance)}` : "";
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(
        clusterId,
      )}/batch-timeseries?metrics=${csv}&${rangeQs(
        rangeOrHours,
      )}${offsetQs}${instanceQs}`,
    ),
  );
  if (!res.ok) throw new Error(`배치 시계열 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`Wait events 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`Slow queries 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`쿼리 상세 조회 실패 (상태 ${res.status})`);
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
  outcome?: { successes: number; attempts: number };
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
  if (!res.ok) throw new Error(`Extensions 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`헬스 점검 항목 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`인덱스 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchVacuumStats(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/vacuum-stats`),
  );
  if (!res.ok) throw new Error(`Vacuum 통계 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`인덱스 추천 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchLongRunningQueries(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/long-running`),
  );
  if (!res.ok) throw new Error(`장기 실행 쿼리 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchBlockingLocks(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/blocking-locks`),
  );
  if (!res.ok) throw new Error(`Locks 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export interface LiveActivity {
  cluster_id: string;
  available: boolean;
  not_applicable?: boolean;
  reason?: string;
  engine_family?: string;
  captured_at?: number;
  sessions?: {
    pid: number;
    usename: string | null;
    state: string | null;
    wait: string | null;
    age_sec: number | null;
    query: string | null;
    backend_type: string | null;
  }[];
  blocking?: { pid: number; blockers: number[] }[];
  db_counters?: Record<string, number>;
  buffercache?: {
    available?: boolean;
    reason?: string;
    used?: number;
    total?: number;
    top_relations?: { relation: string; buffers: number }[];
  } | null;
}

// On-demand LIVE top (P2-⑧). Polled ~2s ONLY while the live view is open;
// `buffers:true` is a one-off manual fetch (the heavy pg_buffercache read) —
// never in the poll loop.
export async function fetchLiveActivity(
  clusterId: string,
  opts: { buffers?: boolean } = {},
): Promise<LiveActivity> {
  const q = opts.buffers ? "?buffers=true" : "";
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/live-activity${q}`),
  );
  if (!res.ok) throw new Error(`라이브 세션 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchClusterSettings(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/settings`),
  );
  if (!res.ok) throw new Error(`설정 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export interface SchemaChangeRow {
  schema_name: string;
  table_name: string;
  /** `created` | `dropped` | `changed` are what api/dashboard/handler.py emits
   * today. Declared as a plain string because this bundle is a static export
   * deployed separately from the api Lambda: a closed union would make the
   * panel's unknown-type cell dead code to the compiler while it is still
   * reachable at runtime, and that cell is the difference between an
   * unrecognised change row and a row that renders nothing at all. */
  change_type: string;
  /** null when the table was never among the 100 largest: UNKNOWN, not zero. */
  baseline_rows: number | string | null;
  current_rows: number | string | null;
  baseline_time: string | null;
  current_time: string | null;
  source?: "schema_snapshots" | "table_stats";
}

/** Every claim in this payload is tied to the producer that can support it, so
 * an empty `changes` never means "nothing changed" on its own: `status`,
 * `ddl_detection`, `row_deltas` and `collection` say which of the two sources
 * was actually able to answer. */
export interface SchemaChangesResponse {
  cluster_id: string;
  days: number;
  /** The HEADLINE. `no_changes` is the only value that means an absence of
   * change, and it requires EVERY source to have compared, across every schema
   * it holds. `partial` is a real negative from one source and silence from the
   * other (a schema whose whole history predates the window, a schema whose
   * history starts inside it, a cache DB without schema_v26, row deltas with only
   * one endpoint). See the state matrix beside the `status` derivation in
   * api/dashboard/handler.py. */
  status:
    | "ok"
    | "no_changes"
    | "partial"
    | "not_collected"
    | "insufficient_history";
  changes: SchemaChangeRow[];
  total_changes: number;
  truncated: boolean;
  note: string;
  ddl_detection: {
    source: string;
    status:
      | "ok"
      | "not_collected"
      /** History exists and NONE of it is comparable: every row predates
       * schema_v27, so no row says which catalog it describes and diffing them
       * would report a phantom DROP for every table the other catalog did not
       * hold. */
      | "not_comparable"
      | "baseline_only"
      | "outside_window"
      | "unavailable";
    schemas_compared: number;
    snapshots_stored: number;
    first_snapshot: string | null;
    last_snapshot: string | null;
    baseline_only_schemas: string[];
    /** Schemas whose snapshot history STARTS inside the window: the pair is real
     * but spans less than `days`, so the answer covers a shorter period than the
     * one asked about. Non-empty here forbids `status: "no_changes"`. */
    partial_window_schemas: string[];
    /** Schemas whose newest snapshot predates the window: the pair would be a
     * row against itself, so nothing inside the window was observed. */
    outside_window_schemas: string[];
    /** Schemas still SERVING TABLES that the newest catalog read did not confirm.
     * THE ACCEPTED COST of this surface: a genuine DROP SCHEMA is never reported
     * as a drop, because absence cannot be told apart from a read that could not
     * reach the schema. It appears here and in `note` as "last confirmed at T, not
     * seen since", and like every other `*_schemas` list it forbids
     * `status: "no_changes"`. */
    unconfirmed_schemas: string[];
    rename_candidates: { from: string; to: string; schema_name: string }[];
  };
  row_deltas: {
    source: string;
    status: "ok" | "no_data" | "insufficient_history";
    tables_compared: number;
    largest_tables_only: number;
  };
  /** WAS EACH SCHEMA STILL THERE, which is a different question from when it last
   * CHANGED. The same block get_schema_diff, get_schema_history and
   * diagnose_root_cause carry, off the same shared probe, so one state is
   * described one way everywhere. */
  observation: {
    status: /** every table-holding schema confirmed under the established scope */
    | "fresh"
      /** at least one is not: see ddl_detection.unconfirmed_schemas */
      | "not_seen"
      /** every stored row predates schema_v27, so nothing is comparable */
      | "unmigrated"
      | "no_snapshots"
      | "unavailable";
    read_scope: string | null;
    last_confirmed: string | null;
    schemas_known: number;
    confirm_within_minutes: number;
  };
  collection: {
    status: "fresh" | "stale" | "no_data";
    last_collected: string | null;
    age_hours: number | null;
    fresh_within_minutes: number;
  };
}

export async function fetchSchemaChanges(
  clusterId: string,
  days = 7,
): Promise<SchemaChangesResponse> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/schema-changes?days=${days}`),
  );
  if (!res.ok) throw new Error(`스키마 변경 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`이상 징후 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`감사 로그 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export interface ChangeImpactDelta {
  metric: string;
  label: string;
  direction: "lower" | "higher" | "neutral";
  before: number;
  after: number;
  delta: number;
  delta_pct: number | null;
}

export interface ChangeImpactEvent {
  event_id: number;
  event_time: string;
  event_type: string;
  message: string;
  window_hours: number;
  deltas: ChangeImpactDelta[];
}

export interface ChangeImpactResponse {
  cluster_id: string;
  window_hours: number;
  days: number;
  changes: ChangeImpactEvent[];
}

// 변경 영향 자동 회고 — RDS 변경 이벤트 전후 워크로드 델타.
export async function fetchChangeImpact(
  clusterId: string,
  windowHours = 2,
  days = 7,
): Promise<ChangeImpactResponse> {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(
        clusterId,
      )}/change-impact?window_hours=${windowHours}&days=${days}`,
    ),
  );
  if (!res.ok) throw new Error(`변경 영향 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`로그 인사이트 조회 실패 (상태 ${res.status})`);
  return res.json();
}

// LOGICAL metric names, the same vocabulary the forecast_capacity MCP tool takes
// (E1-5). Raw metric_type values like storage_bytes are rejected by the endpoint
// with status "unknown_metric": which raw series backs a logical name is
// per-family (Aurora storage GROWS as storage_bytes, standalone RDS storage
// DEPLETES as free_storage_bytes) and the server decides.
export type CapacityMetric =
  | "storage"
  | "connections"
  | "aas"
  | "read_capacity"
  | "write_capacity"
  | "memory";

export type CapacityStatus =
  | "ok"
  | "limit_reached"
  // An LRU/TTL cache is already recycling memory, so a days-to-100% number would
  // be meaningless. NOT an all-clear: the accurate signal is the eviction findings.
  | "evicting"
  | "no_data"
  | "unsupported_metric"
  | "unknown_metric"
  | "unknown_cluster";

export interface CapacityForecastResponse {
  cluster_id: string;
  metric: CapacityMetric;
  status: CapacityStatus;
  label?: string;
  // The raw metric_type the server resolved for this cluster's engine.
  metric_type?: string;
  current_value: number;
  slope_per_day: number;
  r2?: number;
  limit: number;
  limit_basis?: string;
  // false means the ceiling could not be read from the cluster's real config, so
  // no date is asserted.
  grounded?: boolean;
  // Two response modes: "up" grows toward a ceiling, "down" depletes toward 0.
  direction?: "up" | "down";
  // Server-computed 0-100, or null when there is no denominator. NEVER divide by
  // `limit`: it is legitimately 0 in the "down" mode and 0 for an on-demand
  // DynamoDB table.
  usage_pct: number | null;
  days_until_limit: number | null;
  // "Act now?", bounded to an actionable horizon, so a distant ETA reports its
  // date with this false.
  approaching_limit?: boolean;
  forecast: "growing" | "stable" | "shrinking" | "depleting" | "no_data";
  samples: number;
  days_lookback?: number;
  projections?: { d30: number; d60: number; d90: number };
  error?: string;
  // Set alongside every refusing status (unsupported_metric / unknown_metric /
  // unknown_cluster) so an older consumer still renders a notice.
  not_applicable?: boolean;
  engine_family?: string;
  reason?: string | null;
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
  if (!res.ok) throw new Error(`중복 인덱스 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`스키마 그래프 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`SLO 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`토폴로지 조회 실패 (상태 ${res.status})`);
  return res.json();
}

// Engine-level config (read-only) — surfaces config the overview panels don't
// already show. DocumentDB cluster settings + DynamoDB table settings. Returns
// not_applicable for relational (which has the SettingsPanel instead).
export interface EngineConfigResponse {
  cluster_id: string;
  engine_family?: string;
  // true when the family has no engine-config panel (relational) or registry
  // lookup failed.
  not_applicable?: boolean;
  registry_unavailable?: boolean;
  // Friendly fallback on missing/boto error. `info` → neutral notice, not red.
  error?: string;
  info?: boolean;
  // ── DocumentDB ──
  preferred_maintenance_window?: string | null;
  deletion_protection?: boolean;
  storage_encrypted?: boolean;
  db_cluster_parameter_group?: string | null;
  backup_retention_period?: number | null;
  // ── DynamoDB ──
  table_name?: string;
  table_class?: string | null;
  deletion_protection_enabled?: boolean | null;
  sse_type?: string | null;
  sse_status?: string | null;
  stream_enabled?: boolean | null;
  stream_view_type?: string | null;
  ttl_status?: string | null;
  ttl_attribute_name?: string | null;
  // ── ElastiCache ──
  snapshot_retention_limit?: number | null;
  snapshot_window?: string | null;
  at_rest_encryption_enabled?: boolean | null;
  storage_encryption_type?: string | null; // e.g. "sse-elasticache" | "kms"
  transit_encryption_enabled?: boolean | null;
  auth_enabled?: boolean | null; // legacy auth token
  rbac_enabled?: boolean | null; // RBAC user groups (UserGroupIds)
  automatic_failover?: string | null; // "enabled" | "disabled" | "enabling" | ...
  multi_az?: string | null;
  parameter_group?: string | null;
  parameters?: Record<string, string | null>; // key param name → value
}

export interface ActiveSessionsResponse {
  cluster_id: string;
  hours: number;
  samples: Array<{
    ts: string;
    active: number;
    top_wait: string | null;
    top_wait_count: number | null;
  }>;
}

// High-resolution (~5s) active-session samples from the ASH sampler.
export async function fetchActiveSessions(
  clusterId: string,
  hours = 1,
): Promise<ActiveSessionsResponse> {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(clusterId)}/active-sessions?hours=${hours}`,
    ),
  );
  if (!res.ok) throw new Error(`활성 세션 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchEngineConfig(
  clusterId: string,
): Promise<EngineConfigResponse> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/engine-config`),
  );
  if (!res.ok) throw new Error(`구성 정보 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchCapacityForecast(
  clusterId: string,
  metric: CapacityMetric = "storage",
  daysLookback = 30,
): Promise<CapacityForecastResponse> {
  const res = await authedFetch(
    await api(
      `/api/dashboard/${enc(clusterId)}/capacity-forecast?metric=${enc(
        metric,
      )}&days_lookback=${daysLookback}`,
    ),
  );
  if (!res.ok) throw new Error(`용량 예측 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchMultiClusterOverview() {
  const res = await authedFetch(await api(`/api/multi-cluster/overview`));
  if (!res.ok)
    throw new Error(`멀티 클러스터 개요 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchTableSizes(clusterId: string) {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/table-sizes`),
  );
  if (!res.ok) throw new Error(`테이블 크기 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchAlertRules(clusterId?: string) {
  const url = clusterId
    ? await api(`/api/alert-rules?cluster_id=${enc(clusterId)}`)
    : await api(`/api/alert-rules`);
  const res = await authedFetch(url);
  if (!res.ok) throw new Error(`알림 규칙 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`알림 규칙 생성 실패 (상태 ${res.status})`);
  return res.json();
}

export async function updateAlertRule(
  id: number,
  updates: Partial<{
    enabled: boolean;
    threshold: number;
    comparison: AlertComparison;
    name: string;
  }>,
) {
  const res = await authedFetch(await api(`/api/alert-rules/${id}`), {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(updates),
  });
  if (!res.ok) throw new Error(`알림 규칙 수정 실패 (상태 ${res.status})`);
  return res.json();
}

// minutes <= 0 clears the snooze. The evaluator re-checks snooze_until on
// every run, so there's no separate "unsnooze" job to run once it expires.
export async function snoozeAlertRule(id: number, minutes: number) {
  const res = await authedFetch(await api(`/api/alert-rules/${id}/snooze`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ minutes }),
  });
  if (!res.ok) throw new Error(`알림 스누즈 실패 (상태 ${res.status})`);
  return res.json();
}

export async function snoozeAlertRulesByCluster(
  clusterId: string,
  minutes: number,
) {
  const res = await authedFetch(await api(`/api/alert-rules/snooze-bulk`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ cluster_id: clusterId, minutes }),
  });
  if (!res.ok)
    throw new Error(`클러스터 전체 스누즈 실패 (상태 ${res.status})`);
  return res.json();
}

export async function deleteAlertRule(id: number) {
  const res = await authedFetch(await api(`/api/alert-rules/${id}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`알림 규칙 삭제 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`알림 영향 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchAlertSubscriptions() {
  const res = await authedFetch(await api(`/api/alert-subscriptions`));
  if (!res.ok) throw new Error(`알림 구독 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`구독 생성 실패 (상태 ${res.status})`);
  return res.json();
}

export async function deleteAlertSubscription(subArn: string) {
  const res = await authedFetch(
    await api(`/api/alert-subscriptions?sub_arn=${enc(subArn)}`),
    { method: "DELETE", headers: { ...(await authHeaders()) } },
  );
  if (!res.ok) throw new Error(`구독 삭제 실패 (상태 ${res.status})`);
  return res.json();
}

export interface ClusterInstance {
  id: string;
  role: string; // writer | reader
  class: string;
}

export async function fetchClusterInstances(
  clusterId: string,
): Promise<{ instances: ClusterInstance[] }> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/instances`),
  );
  if (!res.ok) throw new Error(`인스턴스 목록 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchClusters() {
  // The cluster list is load-bearing for nearly every page (pickers, the header
  // dropdown, compare's A/B selects). A single transient failure used to be
  // swallowed by callers' catch(() => {}) and left misleading empty states
  // ("no clusters" / "register more clusters") with no retry until a manual
  // reload — so this one call retries briefly before giving up.
  let lastErr: unknown;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt > 0) await new Promise((r) => setTimeout(r, attempt * 1200));
    try {
      const res = await authedFetch(await api(`/api/clusters`));
      if (!res.ok) throw new Error(`클러스터 조회 실패 (상태 ${res.status})`);
      return await res.json();
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr;
}

// Admin-only: set the DB Map note (purpose + connected-service tags). The
// handler gates on the bearer token (viewer => 403).
// The secret-ARN fields are the ONLY channel for the credentials the backend
// whitelists here: db_secret_arn / db_write_secret_arn (rds_instance direct TCP)
// and mongo_secret_arn / mongo_write_secret_arn (DocumentDB deep read + Mongo
// writes). Registration cannot discover them (they are in-DB users), so leaving
// them out of this type made the feature unreachable from the product.
export async function patchClusterMeta(
  clusterId: string,
  meta: {
    purpose?: string;
    service_tags?: string[];
    db_name?: string;
    db_secret_arn?: string;
    db_write_secret_arn?: string;
    mongo_secret_arn?: string;
    mongo_write_secret_arn?: string;
  },
): Promise<void> {
  const res = await authedFetch(
    await api(`/api/clusters/${enc(clusterId)}/meta`),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(meta),
    },
  );
  if (!res.ok) throw new Error(`메타 저장 실패 (상태 ${res.status})`);
}

// ── Agent Tasks — event-driven / scheduled / manual agent work ──────────────

export interface TraceStep {
  step: string;
  tool: string;
  ms: number;
  detail: string;
}

export interface TaskStats {
  total: number;
  by_status: Record<string, number>;
  by_kind: Record<string, number>;
  success_rate: number;
  avg_duration_ms: number;
  recent_failures: number;
}

export interface AgentTask {
  task_id: string;
  cluster_id: string;
  kind: string; // auto_rca | manual_rca | scheduled_report
  trigger: string; // alert:{rule_id} | schedule:{id} | manual:{user}
  status: string; // pending | running | done | failed
  created_at: string; // ms-epoch string
  started_at?: string;
  completed_at?: string;
  title?: string;
  summary?: string;
  error?: string;
  trace?: TraceStep[];
  duration_ms?: number;
  // RCA kinds carry the deterministic diagnose_root_cause payload.
  result?: {
    status?: string;
    anchor_time?: string;
    window_minutes?: number;
    candidates?: Array<{
      rank?: number;
      category?: string;
      summary?: string;
      score?: number;
      when?: string;
      score_breakdown?: Record<string, unknown>;
      [k: string]: unknown;
    }>;
    signals_examined?: Record<string, number>;
    skipped_sources?: string[];
    note?: string;
    [k: string]: unknown;
  };
}

export async function fetchTasks(params?: {
  cluster?: string;
  status?: string;
  limit?: number;
}): Promise<{ tasks: AgentTask[] }> {
  const qs = new URLSearchParams();
  if (params?.cluster) qs.set("cluster", params.cluster);
  if (params?.status) qs.set("status", params.status);
  if (params?.limit) qs.set("limit", String(params.limit));
  const q = qs.toString();
  const res = await authedFetch(await api(`/api/tasks${q ? "?" + q : ""}`));
  if (!res.ok) throw new Error(`작업 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchTask(taskId: string): Promise<AgentTask> {
  const res = await authedFetch(await api(`/api/tasks/${enc(taskId)}`));
  if (!res.ok) throw new Error(`작업 상세 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchTaskStats(): Promise<TaskStats> {
  const res = await authedFetch(await api(`/api/tasks/stats`));
  if (!res.ok) throw new Error(`작업 통계 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function createTask(
  clusterId: string,
  kind = "manual_rca",
): Promise<AgentTask> {
  const res = await authedFetch(await api(`/api/tasks`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({ cluster_id: clusterId, kind }),
  });
  if (!res.ok) {
    let msg = `작업 생성 실패 (상태 ${res.status})`;
    try {
      const e = await res.json();
      if (e?.error) msg = e.error;
    } catch {
      // keep the status-based message
    }
    throw new Error(msg);
  }
  return res.json();
}

// ── Scheduled (recurring) agent tasks ───────────────────────────────────────
export interface AgentSchedule {
  id: number;
  cluster_id: string;
  kind: string;
  interval_kind: string; // hourly | daily | weekly
  enabled: boolean;
  last_run_at?: string | null;
  created_at?: string;
}

export async function fetchSchedules(
  cluster?: string,
): Promise<{ schedules: AgentSchedule[] }> {
  const q = cluster ? `?cluster=${enc(cluster)}` : "";
  const res = await authedFetch(await api(`/api/scheduled-tasks${q}`));
  if (!res.ok) throw new Error(`예약 작업 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function createSchedule(
  clusterId: string,
  intervalKind: string,
  kind = "scheduled_report",
): Promise<AgentSchedule> {
  const res = await authedFetch(await api(`/api/scheduled-tasks`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify({
      cluster_id: clusterId,
      interval_kind: intervalKind,
      kind,
    }),
  });
  if (!res.ok) {
    let msg = `예약 생성 실패 (상태 ${res.status})`;
    try {
      const e = await res.json();
      if (e?.error) msg = e.error;
    } catch {
      // keep the status-based message
    }
    throw new Error(msg);
  }
  return res.json();
}

export async function deleteSchedule(id: number): Promise<void> {
  const res = await authedFetch(await api(`/api/scheduled-tasks/${id}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`예약 삭제 실패 (상태 ${res.status})`);
}

export async function fetchCost(days = 30) {
  const res = await authedFetch(await api(`/api/cost?days=${days}`));
  if (!res.ok) throw new Error(`비용 조회 실패 (상태 ${res.status})`);
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

export interface TokensCost {
  view: string;
  days: number;
  by_model: { model: string; input: number; output: number; total: number }[];
  daily: { date: string; input: number; output: number }[];
  note?: string;
}

export async function fetchCostTokens(days = 30): Promise<TokensCost> {
  const res = await authedFetch(
    await api(`/api/cost?view=tokens&days=${days}`),
  );
  if (!res.ok) throw new Error(`토큰 사용량 조회 실패 (상태 ${res.status})`);
  return res.json() as Promise<TokensCost>;
}

export async function fetchModels() {
  const res = await authedFetch(await api(`/api/models`));
  if (!res.ok) throw new Error(`모델 조회 실패 (상태 ${res.status})`);
  return res.json() as Promise<{
    default: string;
    region: string;
    models: { id: string; label: string; name: string; status: string }[];
    error?: string;
  }>;
}

// ── Scale-out ops (N-④ Phase 2) ─────────────────────────────────────────────
// Scale-out ops are prewarm approval rows (scaleout=true). `state` is the
// server-derived lifecycle; cancel only stops the auto-warm (the reader stays).
export interface ScaleoutOp {
  approval_id: string;
  cluster_id: string;
  reader_instance_id: string | null;
  endpoint_identifier: string;
  top_n: number | null;
  state: string;
  created_at: string;
  warm_dispatched: boolean;
}

export async function fetchScaleoutOps(): Promise<{
  ops: ScaleoutOp[];
  count: number;
}> {
  const res = await authedFetch(await api(`/api/scaleout-ops`));
  if (!res.ok) throw new Error(`스케일 작업 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function cancelScaleoutOp(
  id: string,
): Promise<{ approval_id: string; state: string; note?: string }> {
  const res = await authedFetch(
    await api(`/api/scaleout-ops/${enc(id)}/cancel`),
    { method: "POST", headers: { "Content-Type": "application/json" } },
  );
  if (!res.ok) {
    let msg = `취소 실패 (상태 ${res.status})`;
    try {
      const e = await res.json();
      if (e?.detail || e?.error) msg = e.detail || e.error;
    } catch {
      // keep the status-based message
    }
    throw new Error(msg);
  }
  return res.json();
}

// ── AZ scale-out runbook (P2-⑥) ─────────────────────────────────────────────
// Plans N readers spread over the cluster's healthy AZs (excluding one chosen
// AZ) and mints one add_reader_instance approval (origin="ui") per reader. The
// readers are created only when each approval is approved in the Approval Center.
export interface ScaleoutAzCreated {
  approval_id: string;
  new_instance_id: string;
  availability_zone: string;
  origin_stamped?: boolean;
}

export interface ScaleoutAzResult {
  cluster_id: string;
  exclude_az: string;
  instance_class?: string;
  healthy_azs?: string[];
  created: ScaleoutAzCreated[];
  failed: Array<{
    new_instance_id?: string;
    availability_zone?: string;
    reason?: string;
  }>;
  message: string;
}

export async function scaleoutAz(input: {
  cluster_id: string;
  exclude_az?: string;
  count: number;
  instance_class?: string;
}): Promise<ScaleoutAzResult> {
  const res = await authedFetch(await api(`/api/scaleout-az`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    let msg = `AZ 스케일아웃 실패 (상태 ${res.status})`;
    try {
      const e = await res.json();
      if (e?.detail || e?.error) msg = e.detail || e.error;
    } catch {
      // keep the status-based message
    }
    throw new Error(msg);
  }
  return res.json();
}

export async function registerCluster(data: {
  cluster_id?: string;
  account_id: string;
  region: string;
  engine?: string;
  spoke_role_arn?: string;
  resource_name?: string;
  // DocumentDB only: Mongo-protocol credentials for the in-VPC deep-read
  // collector (read-only user) and the approval-gated Mongo index write.
  // Optional: empty means "no deep read yet", backfillable via PATCH /meta.
  mongo_secret_arn?: string;
  mongo_write_secret_arn?: string;
}) {
  const res = await authedFetch(await api(`/api/clusters`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`클러스터 등록 실패 (상태 ${res.status})`);
  return res.json();
}

export interface TestConnectionResult {
  ok: boolean;
  steps: Array<{
    name:
      | "assume_role"
      | "describe_cluster"
      | "master_user_secret"
      | "data_api";
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
  if (!res.ok) throw new Error(`연결 테스트 실패 (상태 ${res.status})`);
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
      `샘플 생성 실패 (상태 ${res.status}): ${txt.slice(0, 200)}`,
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
  if (!res.ok) throw new Error(`삭제 실패 (상태 ${res.status})`);
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
  // DBOps 자기 자신의 캐시 DB — 자동 선택에서 제외되고 배지가 붙는다.
  is_internal?: boolean;
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
    throw new Error(
      `클러스터 탐색 실패 (상태 ${res.status}): ${txt.slice(0, 200)}`,
    );
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
      `일괄 등록 실패 (상태 ${res.status}): ${txt.slice(0, 200)}`,
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

// MySQL `EXPLAIN FORMAT=JSON` returns a single {query_block: ...} object, NOT an
// array, and shares no keys with the PG shape above. Every cost and percentage
// arrives as a STRING (verified live on Aurora MySQL 8.0.39: query_cost
// "602995.88", filtered "33.33"), so read them through Number(), never as
// numbers. The strategy flags (using_filesort / using_temporary_table) hang off
// the wrappers (ordering_operation / grouping_operation / ...), not off the table
// nodes, and the join order arrives as a nested_loop array.
export type MysqlTableNode = {
  table_name?: string;
  access_type?: string;
  key?: string | null;
  possible_keys?: string[];
  rows_examined_per_scan?: number;
  rows_produced_per_join?: number;
  filtered?: string;
  using_index?: boolean;
  attached_condition?: string;
  cost_info?: { prefix_cost?: string; read_cost?: string; eval_cost?: string };
  [k: string]: unknown;
};

export type MysqlPlanRoot = {
  query_block: {
    cost_info?: { query_cost?: string };
    [k: string]: unknown;
  };
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
  analyze?: boolean,
): Promise<ExplainResponse> {
  const body: Record<string, unknown> = { cluster_id: clusterId, sql };
  if (analyze !== undefined) body.analyze = analyze;
  const res = await authedFetch(await api(`/api/explain`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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
    throw new Error(`EXPLAIN 실패: ${detail.slice(0, 300)}`);
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

// DynamoDB capacity-mode (Provisioned ↔ On-Demand) cost what-if. Dollar fields
// are null when the AWS Price List API couldn't resolve a price (status
// "partial"/"no_data" → fallback) — the UI must render "n/a", never a fake $.
export interface DdbCapacityCostResponse {
  status: "ok" | "partial" | "no_data" | "unsupported";
  cluster_id: string;
  billing_mode: "PROVISIONED" | "PAY_PER_REQUEST" | null;
  region: string;
  window_hours: number;
  datapoints: number;
  no_data_reason?: string;
  unsupported_reason?: string;
  on_demand_monthly_usd?: number | null;
  provisioned_monthly_usd?: number | null;
  current_monthly_usd?: number | null;
  recommended_mode?: "PROVISIONED" | "PAY_PER_REQUEST" | null;
  monthly_savings_usd?: number | null;
  savings_pct?: number | null;
  sizing?: {
    rcu_per_sec: number;
    wcu_per_sec: number;
    basis: string;
    headroom: number;
  };
  pricing_source: "aws_pricing_api" | "fallback";
  assumptions: string[];
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
      `시뮬레이션 요청 실패 (상태 ${res.status}): ${text.slice(0, 200)}`,
    );
  }
  return res.json();
}

export async function fetchParameterCatalog(): Promise<{
  parameters: ParameterCatalogEntry[];
}> {
  const res = await authedFetch(await api(`/api/simulation/parameter-catalog`));
  if (!res.ok)
    throw new Error(`파라미터 카탈로그 조회 실패 (상태 ${res.status})`);
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

export function simulateDynamodbCapacityCost(
  clusterId: string,
  opts?: { headroom?: number; windowHours?: number },
): Promise<DdbCapacityCostResponse> {
  return simPost(`/api/simulation/dynamodb-capacity-cost`, {
    cluster_id: clusterId,
    ...(opts?.headroom != null ? { headroom: opts.headroom } : {}),
    ...(opts?.windowHours != null ? { window_hours: opts.windowHours } : {}),
  });
}

// ElastiCache node-resize cost what-if. Dollar fields are null when the AWS
// Price List API couldn't resolve a price (status "partial") — the UI must
// render "n/a", never a fake $.
export interface ElasticacheNodeResizeResponse {
  status: "ok" | "partial";
  cluster_id: string;
  engine?: string;
  region?: string;
  current: {
    node_type: string | null;
    node_count: number | null;
    price_per_hour: number | null;
  };
  proposed: {
    node_type: string | null;
    node_count: number | null;
    price_per_hour: number | null;
  };
  current_monthly: number | null;
  proposed_monthly: number | null;
  delta_monthly: number | null;
  delta_pct: number | null;
  pricing_source?: "aws_pricing_api" | "fallback";
  note?: string;
}

export function simulateElasticacheNodeResize(
  clusterId: string,
  opts?: { newNodeType?: string; newNodeCount?: number },
): Promise<ElasticacheNodeResizeResponse> {
  return simPost(`/api/simulation/elasticache-node-resize`, {
    cluster_id: clusterId,
    ...(opts?.newNodeType != null ? { new_node_type: opts.newNodeType } : {}),
    ...(opts?.newNodeCount != null
      ? { new_node_count: opts.newNodeCount }
      : {}),
  });
}

// RDS instance (MySQL/SQL Server, non-Aurora) right-sizing + cost what-if.
// Dollar fields are null when the AWS Price List API couldn't resolve a price
// (pricing_source "fallback_estimate") — the UI must render "n/a", never a fake $.
export interface RdsRightsizingResponse {
  status: "ok" | "insufficient_data" | "error" | "unsupported_engine";
  message?: string;
  reason?: string;
  cluster_id?: string;
  engine?: string;
  region?: string;
  current?: {
    instance_class?: string | null;
    storage_gb?: number | null;
    storage_type?: string | null;
    iops?: number | null;
  };
  utilization?: {
    cpu_p95?: number | null;
    conn_peak?: number | null;
    read_iops_p95?: number | null;
    write_iops_p95?: number | null;
    window_hours?: number | null;
  };
  recommendation?: {
    action?: "downsize" | "upsize" | "hold";
    instance_class?: string | null;
    reason?: string;
  };
  cost_impact?: {
    current_monthly_usd?: number | null;
    proposed_monthly_usd?: number | null;
    delta_monthly_usd?: number | null;
    change_pct?: number | null;
    breakdown?: {
      license_note?: string | null;
    };
    pricing_source?: "aws_price_list" | "fallback_estimate";
  };
}

export function simulateRdsInstanceRightsizing(
  clusterId: string,
  opts?: { windowHours?: number; headroom?: number; newInstanceClass?: string },
): Promise<RdsRightsizingResponse> {
  return simPost(`/api/simulation/rds-instance-rightsizing`, {
    cluster_id: clusterId,
    ...(opts?.windowHours != null ? { window_hours: opts.windowHours } : {}),
    ...(opts?.headroom != null ? { headroom: opts.headroom } : {}),
    ...(opts?.newInstanceClass != null
      ? { new_instance_class: opts.newInstanceClass }
      : {}),
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
  if (!res.ok) throw new Error(`런북 목록 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchRunbook(id: number): Promise<RunbookDetail> {
  const res = await authedFetch(await api(`/api/runbooks/${id}`));
  if (!res.ok) throw new Error(`런북 조회 실패 (상태 ${res.status})`);
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
      `런북 생성 실패 (상태 ${res.status}): ${detail.slice(0, 200)}`,
    );
  }
  return res.json();
}

export async function deleteRunbook(id: number): Promise<void> {
  const res = await authedFetch(await api(`/api/runbooks/${id}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`런북 삭제 실패 (상태 ${res.status})`);
}

// =====  Chat sessions (cross-device conversation persistence) =====

export interface ChatSessionSummary {
  session_id: string;
  title: string;
  cluster_id: string;
  updated_at: number;
  created_at: number;
  message_count: number;
  // Added in token-usage tracking (Task 3 ProjectionExpression).
  // Optional: older sessions and any list that pre-dates the backend change
  // will simply omit these fields — the UI guards with ?? 0.
  total_input_tokens?: number;
  total_output_tokens?: number;
  last_error?: { message: string; at: number };
}

export interface ChatSessionDetail extends ChatSessionSummary {
  messages: Array<{
    role: string;
    content: string;
    tool_calls?: unknown[];
    ts?: number;
    followups?: string[];
    incomplete?: boolean;
  }>;
}

export async function listChatSessions(): Promise<ChatSessionSummary[]> {
  const res = await authedFetch(await api(`/api/chat/sessions`), {
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`채팅 세션 목록 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`채팅 세션 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function putChatSession(
  id: string,
  payload: {
    title: string;
    cluster_id: string;
    messages: unknown[];
    total_input_tokens?: number;
    total_output_tokens?: number;
    turn_count?: number;
    last_error?: { message: string; at: number };
  },
): Promise<void> {
  const res = await authedFetch(
    await api(`/api/chat/sessions/${encodeURIComponent(id)}`),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...(await authHeaders()) },
      body: JSON.stringify(payload),
    },
  );
  if (!res.ok) throw new Error(`채팅 세션 저장 실패 (상태 ${res.status})`);
}

export async function deleteChatSession(id: string): Promise<void> {
  const res = await authedFetch(
    await api(`/api/chat/sessions/${encodeURIComponent(id)}`),
    {
      method: "DELETE",
      headers: { ...(await authHeaders()) },
    },
  );
  if (!res.ok) throw new Error(`채팅 세션 삭제 실패 (상태 ${res.status})`);
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
  if (!res.ok)
    throw new Error(`저장된 쿼리 목록 조회 실패 (상태 ${res.status})`);
  const body = await res.json();
  return body.queries || [];
}

export async function fetchSavedQuery(id: number): Promise<SavedQueryDetail> {
  const res = await authedFetch(await api(`/api/saved-queries/${id}`));
  if (!res.ok) throw new Error(`저장된 쿼리 조회 실패 (상태 ${res.status})`);
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
      `쿼리 저장 실패 (상태 ${res.status}): ${detail.slice(0, 200)}`,
    );
  }
  return res.json();
}

export async function deleteSavedQuery(id: number): Promise<void> {
  const res = await authedFetch(await api(`/api/saved-queries/${id}`), {
    method: "DELETE",
    headers: { ...(await authHeaders()) },
  });
  if (!res.ok) throw new Error(`저장된 쿼리 삭제 실패 (상태 ${res.status})`);
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

export async function fetchResourceDetails(clusterId: string): Promise<{
  engine: string;
  engine_family: string;
  resource_details: Record<string, unknown> | null;
}> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/resource-details`),
  );
  if (!res.ok) throw new Error(`리소스 상세 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchHealth(): Promise<HealthResponse> {
  const res = await authedFetch(await api(`/api/health`));
  if (!res.ok) throw new Error(`헬스 상태 조회 실패 (상태 ${res.status})`);
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
  cursor?: string;
  exportMode?: boolean;
}): Promise<{
  items: ActivityItem[];
  count: number;
  next_cursor?: string | null;
}> {
  const params = new URLSearchParams();
  if (opts?.cluster_id) params.set("cluster_id", opts.cluster_id);
  if (opts?.actor) params.set("actor", opts.actor);
  if (opts?.action_type) params.set("action_type", opts.action_type);
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.cursor) params.set("cursor", opts.cursor);
  if (opts?.exportMode) params.set("export", "true");
  const qs = params.toString();
  const url = await api(`/api/activity${qs ? `?${qs}` : ""}`);
  const res = await authedFetch(url);
  if (!res.ok) throw new Error(`활동 조회 실패 (상태 ${res.status})`);
  return res.json();
}

// Loop the cursor-paginated export until exhausted, accumulating all rows.
// Continues while next_cursor is non-null (a filtered DDB page can be empty
// yet still have more pages). A hard page ceiling bounds a runaway loop.
export async function fetchAllActivity(opts?: {
  cluster_id?: string;
  actor?: string;
  action_type?: string;
}): Promise<{ items: ActivityItem[]; capped: boolean }> {
  const MAX_PAGES = 200;
  const MAX_ROWS = 50000;
  const items: ActivityItem[] = [];
  let cursor: string | undefined = undefined;
  let pages = 0;
  for (;;) {
    const page = await fetchActivity({
      ...opts,
      exportMode: true,
      limit: 1000,
      cursor,
    });
    items.push(...page.items);
    pages += 1;
    const next = page.next_cursor;
    // Exhausted → complete, regardless of count (even exactly MAX_ROWS is NOT
    // capped when there is no next page). Check this BEFORE the ceilings.
    if (!next) return { items, capped: false };
    // More data exists but we stop at a ceiling → slice to the exact row cap
    // so the in-memory result never overshoots by a page, and mark it capped.
    if (pages >= MAX_PAGES || items.length >= MAX_ROWS) {
      return { items: items.slice(0, MAX_ROWS), capped: true };
    }
    cursor = next;
  }
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
  // Engine-family tag (relational | documentdb | dynamodb). Absent on older
  // relational responses → treated as relational by the panel.
  engine_family?: string;
  // DynamoDB-only: PITR posture + on-demand backups (no RDS-style snapshots).
  table_name?: string;
  pitr_enabled?: boolean;
  on_demand_backups?: DdbOnDemandBackup[];
  on_demand_count?: number;
}

export interface DdbOnDemandBackup {
  name: string | null;
  status: string | null;
  created: string | null;
  size_bytes: number | null;
  type: string | null;
}

export async function fetchBackups(
  clusterId: string,
): Promise<BackupsResponse> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}/backups`),
  );
  if (!res.ok) throw new Error(`백업 조회 실패 (상태 ${res.status})`);
  return res.json();
}

// Cluster endpoints panel (P2-⑤): built-in writer/reader + custom endpoints.
export interface ClusterEndpoint {
  identifier: string | null;
  type: string | null; // WRITER | READER | CUSTOM
  custom_type: string | null; // READER | ANY (custom only)
  status: string | null;
  endpoint: string | null;
  static_members: string[];
  excluded_members: string[];
}

export interface EndpointsResponse {
  cluster_id: string;
  engine?: string;
  custom_count?: number;
  endpoints: ClusterEndpoint[];
  checked_at?: number;
  error?: string;
  info?: boolean;
  not_applicable?: boolean;
  engine_family?: string;
  registry_unavailable?: boolean;
}

// Rides the base dashboard route via ?view=endpoints (no dedicated route).
export async function fetchEndpoints(
  clusterId: string,
): Promise<EndpointsResponse> {
  const res = await authedFetch(
    await api(`/api/dashboard/${enc(clusterId)}?view=endpoints`),
  );
  if (!res.ok) throw new Error(`엔드포인트 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export type EndpointAction =
  | "create_custom_endpoint"
  | "modify_custom_endpoint"
  | "delete_custom_endpoint";

export interface EndpointRequestResponse {
  approval_id: string;
  cluster_id: string;
  action: EndpointAction;
  message: string;
}

// N-① console-initiated custom-endpoint write. Admin-gated. Does NOT mutate
// the endpoint immediately — it mints a payload-hashed approval that runs when
// the DBA approves it in the Approval Center. static_members and
// excluded_members are mutually exclusive (send at most one).
export async function createEndpointRequest(opts: {
  clusterId: string;
  action: EndpointAction;
  endpointIdentifier: string;
  endpointType?: "READER" | "ANY"; // create only
  staticMembers?: string[];
  excludedMembers?: string[];
}): Promise<EndpointRequestResponse> {
  const body: Record<string, unknown> = {
    cluster_id: opts.clusterId,
    action: opts.action,
    endpoint_identifier: opts.endpointIdentifier,
  };
  if (opts.action === "create_custom_endpoint") {
    body.endpoint_type = opts.endpointType;
  }
  if (opts.staticMembers?.length) body.static_members = opts.staticMembers;
  if (opts.excludedMembers?.length)
    body.excluded_members = opts.excludedMembers;
  const res = await authedFetch(await api(`/api/endpoint-requests`), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(await authHeaders()) },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(
      `승인 요청 실패 (상태 ${res.status}): ${detail.slice(0, 200)}`,
    );
  }
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
      `스냅샷 생성 실패 (상태 ${res.status}): ${detail.slice(0, 200)}`,
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
    throw new Error(`복원 실패 (상태 ${res.status}): ${detail.slice(0, 300)}`);
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
  if (!res.ok) throw new Error(`Workload diff 조회 실패 (상태 ${res.status})`);
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
  /** Signal streams whose query FAILED (a cache DB without schema_v26, a
   * missing audit_log). A category absent from `categories` while it is named
   * here is UNKNOWN, not "that signal did not fire", which on a timeline is the
   * whole difference between "no DDL during the incident" and "we could not
   * look". Absent on a payload from an api Lambda older than this field. */
  degraded_sources?: string[];
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
  if (!res.ok) throw new Error(`타임라인 조회 실패 (상태 ${res.status})`);
  return res.json();
}

// =====  Hover-prefetch — warm the browser HTTP cache before navigation =====
//
// The dashboard panel APIs are served with Cache-Control: max-age=30,
// stale-while-revalidate=120. Fetching them on hover (before the click) means
// the click hits the warm browser cache → instant first paint.
//
// Strategy: fire the same two calls the dashboard page makes on cluster select
// so the browser caches them under the identical URLs.
//   1. fetchDashboard(clusterId)          — cluster meta + top queries + events
//   2. fetchBatchTimeseries(clusterId, PREFETCH_CHART_METRICS, DEFAULT_HOURS)
//      — the 10-metric batch the timeseries charts read
//
// Dedupe: skip if the same cluster was prefetched within 10 s to avoid
// hover-spam on fast mouse movements across a dense fleet table.

// Must match the CHART_METRICS array in dashboard/page.tsx exactly so the
// URLs are identical (same query string → same browser cache entry).
const PREFETCH_CHART_METRICS = [
  "aas",
  "cpu",
  "db_connections",
  "read_iops",
  "write_iops",
  "xact_commit",
  "tup_returned",
  "storage_bytes",
  "replica_lag_ms",
  "deadlocks",
];

// Must match DEFAULT_RANGE in dashboard/page.tsx: { kind: "preset", hours: 1 }
const PREFETCH_DEFAULT_RANGE: TimeRange = { kind: "preset", hours: 1 };

// clusterId → timestamp of last prefetch (ms). Module-level so it persists
// across renders but resets on full page load (acceptable).
const _prefetchTimestamps = new Map<string, number>();
const PREFETCH_DEDUPE_MS = 10_000; // 10 seconds

/**
 * Fire-and-forget prefetch of the dashboard's two primary data calls.
 * Call this on cluster hover/focus. Never throws, never blocks the UI.
 * Returns void — callers should not await or chain on this.
 */
export function prefetchDashboard(clusterId: string): void {
  if (!clusterId) return;
  const now = Date.now();
  const last = _prefetchTimestamps.get(clusterId);
  if (last !== undefined && now - last < PREFETCH_DEDUPE_MS) return;
  _prefetchTimestamps.set(clusterId, now);

  // Fire both calls in parallel. Errors are silently swallowed — the only
  // purpose is populating the browser HTTP cache; UI never reads the result.
  fetchDashboard(clusterId).catch(() => {});
  fetchBatchTimeseries(
    clusterId,
    PREFETCH_CHART_METRICS,
    PREFETCH_DEFAULT_RANGE,
  ).catch(() => {});
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
  if (!res.ok) throw new Error(`메모리 목록 조회 실패 (상태 ${res.status})`);
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
  if (!res.ok) throw new Error(`메모리 삭제 실패 (상태 ${res.status})`);
}

// =====  Approval policies (designated-approver routing, admin-gated) =====

export interface ApprovalPolicy {
  policy_id: string;
  cluster_id: string;
  action_type: string;
  approvers: string[];
  description: string;
  updated_at?: string;
  updated_by?: string;
}

export async function fetchApprovalPolicies(): Promise<{
  policies: ApprovalPolicy[];
}> {
  const res = await authedFetch(await apiUrl("/api/approval-policies"));
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`policy fetch failed: ${res.status}`);
  return res.json();
}

async function _writePolicy(
  method: "POST" | "PUT",
  body: Partial<ApprovalPolicy>,
  id?: string,
): Promise<ApprovalPolicy> {
  const path = id
    ? `/api/approval-policies/${encodeURIComponent(id)}`
    : "/api/approval-policies";
  const res = await authedFetch(await apiUrl(path), {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) {
    let msg = `policy save failed: ${res.status}`;
    try {
      const b = await res.json();
      if (b?.error) msg = b.error;
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }
  return res.json();
}

export function createApprovalPolicy(
  body: Partial<ApprovalPolicy>,
): Promise<ApprovalPolicy> {
  return _writePolicy("POST", body);
}

export function updateApprovalPolicy(
  id: string,
  body: Partial<ApprovalPolicy>,
): Promise<ApprovalPolicy> {
  return _writePolicy("PUT", body, id);
}

export async function deleteApprovalPolicy(id: string): Promise<void> {
  const res = await authedFetch(
    await apiUrl(`/api/approval-policies/${encodeURIComponent(id)}`),
    {
      method: "DELETE",
    },
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`policy delete failed: ${res.status}`);
}

// =====  App-level feature config (admin-gated) =====

export interface AppConfigItem {
  key: string;
  value: string;
  default: string;
  updated_at: string | null;
  updated_by: string | null;
}

export async function fetchAppConfig(): Promise<{ items: AppConfigItem[] }> {
  const res = await authedFetch(await apiUrl("/api/config"));
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`config fetch failed: ${res.status}`);
  return res.json();
}

export async function updateAppConfig(
  config: Record<string, string | boolean>,
): Promise<{ items: AppConfigItem[] }> {
  const res = await authedFetch(await apiUrl("/api/config"), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) {
    let msg = `config update failed: ${res.status}`;
    try {
      const b = await res.json();
      if (b?.error) msg = b.error;
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }
  return res.json();
}

// =====  Operator context files (admin-gated) =====

export interface ContextFile {
  file_id: string;
  name: string;
  content: string;
  content_type: string;
  size: number;
  updated_at?: string;
  updated_by?: string;
}

export async function fetchContextFiles(): Promise<{ items: ContextFile[] }> {
  const res = await authedFetch(await apiUrl("/api/context-files"));
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`context-files fetch failed: ${res.status}`);
  return res.json();
}

export async function uploadContextFile(body: {
  name: string;
  content: string;
  content_type: string;
}): Promise<ContextFile> {
  const res = await authedFetch(await apiUrl("/api/context-files"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) {
    let msg = `upload failed: ${res.status}`;
    try {
      const b = await res.json();
      if (b?.error) msg = b.error;
    } catch {
      /* keep */
    }
    throw new Error(msg);
  }
  return res.json();
}

export async function deleteContextFile(id: string): Promise<void> {
  const res = await authedFetch(
    await apiUrl(`/api/context-files/${encodeURIComponent(id)}`),
    { method: "DELETE" },
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`delete failed: ${res.status}`);
}

// =====  Admin user/role management (admin-gated)  =====

export interface AdminUser {
  username: string;
  email: string | null;
  status: string | null;
  enabled: boolean;
  created: string | null;
  role: "admin" | "viewer";
  implicit: boolean;
}

export async function fetchAdminUsers(
  cursor?: string,
): Promise<{ items: AdminUser[]; next_cursor: string | null }> {
  const path = cursor
    ? `/api/admin/users?cursor=${enc(cursor)}`
    : "/api/admin/users";
  const res = await authedFetch(await apiUrl(path));
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`사용자 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function updateUserRole(
  username: string,
  role: "admin" | "viewer",
): Promise<{ username: string; role: string }> {
  const res = await authedFetch(
    await apiUrl(`/api/admin/users/${enc(username)}/role`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role }),
    },
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) {
    let msg = `역할 변경 실패 (상태 ${res.status})`;
    try {
      const b = await res.json();
      if (b?.error) msg = b.error;
    } catch {
      /* keep default */
    }
    throw new Error(msg);
  }
  return res.json();
}

// =====  Admin team management (admin-gated)  =====

export interface AdminTeam {
  team_id: string;
  name: string;
  created_at?: string;
  created_by?: string;
  member_count: number;
}

export interface TeamDetail {
  team_id: string;
  name: string;
  members: string[];
  clusters: string[];
}

export async function fetchAdminTeams(): Promise<{ teams: AdminTeam[] }> {
  const res = await authedFetch(await apiUrl("/api/admin/teams"));
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`팀 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function fetchTeamDetail(teamId: string): Promise<TeamDetail> {
  const res = await authedFetch(
    await apiUrl(`/api/admin/teams/${enc(teamId)}`),
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`팀 상세 조회 실패 (상태 ${res.status})`);
  return res.json();
}

export async function createTeam(
  name: string,
): Promise<{ team_id: string; name: string }> {
  const res = await authedFetch(await apiUrl("/api/admin/teams"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`팀 생성 실패 (상태 ${res.status})`);
  return res.json();
}

export async function deleteTeam(
  teamId: string,
): Promise<{ team_id: string; deleted: boolean }> {
  const res = await authedFetch(
    await apiUrl(`/api/admin/teams/${enc(teamId)}`),
    { method: "DELETE" },
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`팀 삭제 실패 (상태 ${res.status})`);
  return res.json();
}

async function _teamMutate(
  path: string,
  method: "POST" | "DELETE",
  failMsg: string,
): Promise<void> {
  const res = await authedFetch(await apiUrl(path), { method });
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok) throw new Error(`${failMsg} (상태 ${res.status})`);
}

export async function addTeamMember(
  teamId: string,
  username: string,
): Promise<void> {
  return _teamMutate(
    `/api/admin/teams/${enc(teamId)}/members/${enc(username)}`,
    "POST",
    "멤버 추가 실패",
  );
}

export async function removeTeamMember(
  teamId: string,
  username: string,
): Promise<void> {
  return _teamMutate(
    `/api/admin/teams/${enc(teamId)}/members/${enc(username)}`,
    "DELETE",
    "멤버 제거 실패",
  );
}

export async function assignClusterToTeam(
  teamId: string,
  clusterId: string,
): Promise<void> {
  return _teamMutate(
    `/api/admin/teams/${enc(teamId)}/clusters/${enc(clusterId)}`,
    "POST",
    "클러스터 할당 실패",
  );
}

export async function unassignClusterFromTeam(
  teamId: string,
  clusterId: string,
): Promise<void> {
  return _teamMutate(
    `/api/admin/teams/${enc(teamId)}/clusters/${enc(clusterId)}`,
    "DELETE",
    "클러스터 할당 해제 실패",
  );
}

// ---------------------------------------------------------------------------
// Onboarding — spoke-account CloudFormation template
// ---------------------------------------------------------------------------

export interface OnboardingTemplate {
  template: string;
  hub_account_id: string;
  hub_role_arn: string;
  role_name: string;
  remediation: boolean;
  region: string | null;
}

// ── Remediation outcome learning ────────────────────────────────────────────

export interface AggRow {
  cluster_id?: string;
  symptom_class: string;
  action_class: string;
  successes: number;
  attempts: number;
  last_outcome: string | null;
}

// Backend emits: resolved | persisted | inconclusive (evaluator.py)
export type RecentStatus = "resolved" | "persisted" | "inconclusive";

export interface RecentCase {
  cluster_id: string;
  symptom_class: string;
  action_class: string;
  status: RecentStatus;
  evaluated_at: string;
}

export async function fetchLearning(): Promise<{
  fleet: AggRow[];
  clusters: Record<string, AggRow[]>;
  recent: RecentCase[];
}> {
  return authedFetch(await api(`/api/learning`)).then((r) => r.json());
}

export async function fetchOnboardingTemplate(opts?: {
  region?: string;
  remediation?: boolean;
}): Promise<OnboardingTemplate> {
  const p = new URLSearchParams();
  if (opts?.region) p.set("region", opts.region);
  if (opts?.remediation) p.set("remediation", "true");
  const qs = p.toString();
  const res = await authedFetch(
    await apiUrl(`/api/onboarding/template${qs ? `?${qs}` : ""}`),
  );
  if (res.status === 403) throw new Error("admin only");
  if (!res.ok)
    throw new Error(`onboarding template fetch failed: ${res.status}`);
  return res.json();
}
