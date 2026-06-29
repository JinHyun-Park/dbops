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

export type StatusLevel = "ok" | "warning" | "critical";

const CRITICAL_STATUS =
  /(stopped|failed|deleting|inaccessible|incompatible|error)/i;
const WARNING_STATUS =
  /(modifying|backing-?up|maintenance|upgrading|storage-optimization|configuring|impaired|resetting|migrating|starting)/i;

/** Coarse availability level for the Map's status dot, from the registry/cache
 *  status (the Map reads /api/clusters, which carries status but not live
 *  metrics — full health is one click away on the dashboard). Unknown/healthy
 *  statuses read as ok so the Map never false-alarms. */
export function statusLevel(c: MapCluster): StatusLevel {
  const s = `${c.status || ""} ${c.connection_status || ""}`.toLowerCase();
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
