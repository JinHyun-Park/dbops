// Pure helpers for the DB Map (service-blueprint) view. No React, no fetch — so
// they're trivially unit-testable and reusable.

export interface MapCluster {
  cluster_id: string;
  engine?: string;
  region?: string;
  team_id?: string;
  status?: string;
  connection_status?: string;
  purpose?: string;
  service_tags?: string[];
  vpc_id?: string;
  availability_zones?: string; // comma-joined AZ names
  resource_name?: string;
}

export type EnvKind = "prod" | "staging" | "dev";

const ENV_PATTERNS: { env: EnvKind; re: RegExp }[] = [
  // Word-boundary-ish matches so "production-db" tags prod but "provider" doesn't.
  { env: "prod", re: /(^|[^a-z])(prod|prd|production|live)([^a-z]|$)/i },
  { env: "staging", re: /(^|[^a-z])(stag|stg|staging|preprod|uat)([^a-z]|$)/i },
  {
    env: "dev",
    re: /(^|[^a-z])(dev|test|qa|sandbox|sbx|demo|scratch)([^a-z]|$)/i,
  },
];

/** Infer prod/staging/dev from the cluster id/name + tags. Display-only, never
 *  written. Conservative: returns null unless a clear token matches (prod wins
 *  over staging over dev when several appear). */
export function inferEnv(
  name: string | undefined,
  tags?: string[],
): EnvKind | null {
  const hay = [name || "", ...(tags || [])].join(" ");
  for (const { env, re } of ENV_PATTERNS) {
    if (re.test(hay)) return env;
  }
  return null;
}

export type StatusLevel = "ok" | "warning" | "critical" | "unknown";

const CRITICAL_STATUS =
  /(stopped|failed|deleting|inaccessible|incompatible|error)/i;
const WARNING_STATUS =
  /(modifying|backing-?up|maintenance|upgrading|storage-optimization|configuring|impaired|resetting|migrating|starting)/i;

/** Coarse availability level for the Map's status dot, from the registry/cache
 *  status (the Map reads /api/clusters, which carries status but not live
 *  metrics — full health is one click away on the dashboard). An EMPTY status
 *  (not collected yet) reads as "unknown" (neutral gray) rather than false-green;
 *  a recognized non-critical/non-warning status reads as ok. */
export function statusLevel(c: MapCluster): StatusLevel {
  const s = `${c.status || ""} ${c.connection_status || ""}`
    .trim()
    .toLowerCase();
  if (!s) return "unknown"; // no status signal yet (e.g., before the first collection)
  if (CRITICAL_STATUS.test(s)) return "critical";
  if (WARNING_STATUS.test(s)) return "warning";
  return "ok";
}

export const UNASSIGNED = "Unassigned";

export interface ServiceGroup {
  service: string;
  clusters: MapCluster[];
}

/** Group clusters by service_tags (the blueprint's organizing axis). A cluster
 *  with multiple tags appears under each; one with none lands in "Unassigned".
 *  Named services are sorted alphabetically; "Unassigned" always comes last. */
export function groupByService(clusters: MapCluster[]): ServiceGroup[] {
  const byService = new Map<string, MapCluster[]>();
  for (const c of clusters) {
    const tags = (c.service_tags || []).map((t) => t.trim()).filter(Boolean);
    const keys = tags.length ? Array.from(new Set(tags)) : [UNASSIGNED];
    for (const k of keys) {
      const arr = byService.get(k) || [];
      arr.push(c);
      byService.set(k, arr);
    }
  }
  const groups = Array.from(byService.entries()).map(([service, cs]) => ({
    service,
    clusters: cs
      .slice()
      .sort((a, b) => a.cluster_id.localeCompare(b.cluster_id)),
  }));
  groups.sort((a, b) => {
    if (a.service === UNASSIGNED) return 1;
    if (b.service === UNASSIGNED) return -1;
    return a.service.localeCompare(b.service);
  });
  return groups;
}

export interface VpcGroup {
  region: string;
  vpcId: string | null; // null = serverless / no VPC (DynamoDB, or not yet collected)
  azs: string[];
  clusters: MapCluster[];
}

/** Nest clusters by region → VPC for the architecture view. Clusters with no
 *  vpc_id (DynamoDB = serverless; or VPC not yet collected) bucket into a
 *  per-region serverless group (vpcId = null). Sorted: region asc, then VPC id
 *  asc with the serverless bucket last within each region. */
export function groupByVpc(clusters: MapCluster[]): VpcGroup[] {
  const map = new Map<string, VpcGroup>();
  for (const c of clusters) {
    const region = c.region || "unknown";
    const vpcId = c.vpc_id || null;
    const key = `${region}::${vpcId ?? ""}`;
    let g = map.get(key);
    if (!g) {
      const azs = (c.availability_zones || "")
        .split(",")
        .map((a) => a.trim())
        .filter(Boolean);
      g = { region, vpcId, azs, clusters: [] };
      map.set(key, g);
    } else if (g.azs.length === 0 && c.availability_zones) {
      g.azs = c.availability_zones
        .split(",")
        .map((a) => a.trim())
        .filter(Boolean);
    }
    g.clusters.push(c);
  }
  const groups = Array.from(map.values());
  for (const g of groups) {
    g.clusters.sort((a, b) => a.cluster_id.localeCompare(b.cluster_id));
  }
  groups.sort((a, b) => {
    if (a.region !== b.region) return a.region.localeCompare(b.region);
    if (a.vpcId === null) return 1; // serverless bucket last within a region
    if (b.vpcId === null) return -1;
    return a.vpcId.localeCompare(b.vpcId);
  });
  return groups;
}
