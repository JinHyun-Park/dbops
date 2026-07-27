// Engine helpers + Aurora version EOL schedule.
//
// AWS engine strings observed in the wild: "aurora-postgresql", "aurora-mysql".
// Treat anything else conservatively (UNKNOWN) so we don't accidentally hide
// PG-only panels on a future engine variant.

export type EngineKind =
  | "postgres"
  | "mysql"
  | "sqlserver"
  | "docdb"
  | "dynamodb"
  | "elasticache"
  | "unknown";

export function engineKind(engine: string | null | undefined): EngineKind {
  const e = (engine || "").toLowerCase();
  if (e.includes("postgres")) return "postgres";
  if (e.includes("sqlserver")) return "sqlserver";
  if (e.includes("mysql")) return "mysql";
  if (e.includes("docdb")) return "docdb";
  if (e.includes("dynamodb")) return "dynamodb";
  if (
    e.includes("redis") ||
    e.includes("valkey") ||
    e.includes("memcached") ||
    e.includes("elasticache")
  )
    return "elasticache";
  return "unknown";
}

export function isPostgres(engine: string | null | undefined): boolean {
  return engineKind(engine) === "postgres";
}

export function isMysql(engine: string | null | undefined): boolean {
  return engineKind(engine) === "mysql";
}

export interface EngineBadge {
  label: string;
  short: string;
  classes: string; // tailwind classes for bg/text/border
  accent: string; // for accent dots / pills
}

export function engineBadge(engine: string | null | undefined): EngineBadge {
  switch (engineKind(engine)) {
    case "postgres":
      return {
        label: "PostgreSQL",
        short: "PG",
        classes: "bg-sky-500/15 text-sky-300 border-sky-500/40",
        accent: "bg-sky-400",
      };
    case "mysql":
      return {
        label: "MySQL",
        short: "MySQL",
        classes: "bg-orange-500/15 text-orange-300 border-orange-500/40",
        accent: "bg-orange-400",
      };
    case "sqlserver":
      return {
        label: "SQL Server",
        short: "MSSQL",
        classes: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40",
        accent: "bg-indigo-400",
      };
    case "docdb":
      return {
        label: "DocumentDB",
        short: "DocDB",
        classes: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
        accent: "bg-emerald-400",
      };
    case "dynamodb":
      return {
        label: "DynamoDB",
        short: "DDB",
        classes: "bg-purple-500/15 text-purple-300 border-purple-500/40",
        accent: "bg-purple-400",
      };
    case "elasticache": {
      // ElastiCache covers Redis/Valkey/Memcached — reflect the specific engine
      // in the badge while sharing the family's rose accent.
      const e = (engine || "").toLowerCase();
      const label = e.includes("memcached")
        ? "Memcached"
        : e.includes("valkey")
          ? "Valkey"
          : e.includes("redis")
            ? "Redis"
            : "ElastiCache";
      const short = e.includes("memcached")
        ? "MC"
        : label === "ElastiCache"
          ? "Cache"
          : label;
      return {
        label,
        short,
        classes: "bg-rose-500/15 text-rose-300 border-rose-500/40",
        accent: "bg-rose-400",
      };
    }
    default:
      return {
        label: engine || "unknown",
        short: "?",
        classes: "bg-zinc-700/30 text-zinc-400 border-zinc-700",
        accent: "bg-zinc-500",
      };
  }
}

// Major-version EOL schedule. Sourced from AWS RDS/Aurora release notes
// (Aurora MySQL: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraMySQLReleaseNotes/Welcome.html,
//  Aurora PostgreSQL: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/AuroraPostgreSQL.Updates.html).
// "Standard support" end-of-life — after this date AWS RDS Extended Support kicks
// in at extra cost. We surface this so DBAs upgrade ahead of the cliff.
//
// Update annually as new majors GA and old ones EOL.
//
// Match by version major prefix (PG) or aurora variant (MySQL); keep this
// table small — we don't need every minor. Tighter dates win (the row's
// `version_prefix` is matched against the cluster's reported engine_version
// using startsWith after a normalized lowercase compare).
interface EolEntry {
  engine: EngineKind;
  version_prefix: string;
  display_name: string;
  eol: string; // YYYY-MM-DD
  note?: string;
}

const EOL_TABLE: EolEntry[] = [
  // --- Aurora PostgreSQL — standard support end-of-life ---
  {
    engine: "postgres",
    version_prefix: "11.",
    display_name: "PostgreSQL 11",
    eol: "2024-02-29",
    note: "Extended Support charges apply",
  },
  {
    engine: "postgres",
    version_prefix: "12.",
    display_name: "PostgreSQL 12",
    eol: "2025-02-28",
    note: "Extended Support charges apply",
  },
  {
    engine: "postgres",
    version_prefix: "13.",
    display_name: "PostgreSQL 13",
    eol: "2026-02-28",
  },
  {
    engine: "postgres",
    version_prefix: "14.",
    display_name: "PostgreSQL 14",
    eol: "2026-11-12",
  },
  {
    engine: "postgres",
    version_prefix: "15.",
    display_name: "PostgreSQL 15",
    eol: "2027-11-11",
  },
  {
    engine: "postgres",
    version_prefix: "16.",
    display_name: "PostgreSQL 16",
    eol: "2028-11-09",
  },
  {
    engine: "postgres",
    version_prefix: "17.",
    display_name: "PostgreSQL 17",
    eol: "2029-11-08",
  },

  // --- Aurora MySQL — version strings look like "5.7.mysql_aurora.2.x" or "8.0.mysql_aurora.3.x" ---
  {
    engine: "mysql",
    version_prefix: "5.7",
    display_name: "MySQL 5.7 (Aurora v2)",
    eol: "2024-10-31",
    note: "Aurora MySQL v2 — Extended Support active",
  },
  {
    engine: "mysql",
    version_prefix: "8.0",
    display_name: "MySQL 8.0 (Aurora v3)",
    eol: "2027-04-30",
  },
  {
    engine: "mysql",
    version_prefix: "8.4",
    display_name: "MySQL 8.4 (Aurora v4)",
    eol: "2032-04-30",
  },
];

export interface EolInfo {
  display_name: string;
  eol: string;
  days_remaining: number;
  status: "expired" | "imminent" | "soon" | "safe";
  note?: string;
}

export function eolFor(
  engine: string | null | undefined,
  version: string | null | undefined,
  now: Date = new Date(),
): EolInfo | null {
  const kind = engineKind(engine);
  if (kind === "unknown" || !version) return null;
  const v = version.toLowerCase();
  // Find the longest matching prefix so PG 15.x outranks a hypothetical PG 1.x row.
  let match: EolEntry | null = null;
  for (const entry of EOL_TABLE) {
    if (entry.engine !== kind) continue;
    if (v.startsWith(entry.version_prefix.toLowerCase())) {
      if (!match || entry.version_prefix.length > match.version_prefix.length) {
        match = entry;
      }
    }
  }
  if (!match) return null;
  const eolDate = new Date(match.eol + "T00:00:00Z");
  const days = Math.floor(
    (eolDate.getTime() - now.getTime()) / (24 * 3600 * 1000),
  );
  let status: EolInfo["status"];
  if (days < 0) status = "expired";
  else if (days < 90) status = "imminent";
  else if (days < 365) status = "soon";
  else status = "safe";
  return {
    display_name: match.display_name,
    eol: match.eol,
    days_remaining: days,
    status,
    note: match.note,
  };
}

export const EOL_STATUS_CLASSES: Record<EolInfo["status"], string> = {
  expired: "text-rose-400",
  imminent: "text-rose-400",
  soon: "text-amber-400",
  safe: "text-emerald-400",
};

export function eolHint(info: EolInfo): string {
  if (info.status === "expired") {
    return `${info.display_name} reached EOL ${Math.abs(
      info.days_remaining,
    )} days ago (${info.eol})${info.note ? " — " + info.note : ""}`;
  }
  return `${info.display_name} EOL on ${info.eol} (${
    info.days_remaining
  } days remaining)${info.note ? " — " + info.note : ""}`;
}

// --- Finer-grained engine groups for DISPLAY/enumeration ---
// Relational splits into PG vs MySQL here. Capability gating still uses
// engineFamily — PG and MySQL are both "relational" → same SQL panels.
export type EngineGroup =
  | "aurora-postgresql"
  | "aurora-mysql"
  | "rds-mysql"
  | "rds-sqlserver"
  | "documentdb"
  | "dynamodb"
  | "elasticache";

export function engineGroup(engine: string | null | undefined): EngineGroup {
  const fam = engineFamily(engine);
  if (fam === "documentdb") return "documentdb";
  if (fam === "dynamodb") return "dynamodb";
  if (fam === "elasticache") return "elasticache";
  if (fam === "rds_instance")
    return engineKind(engine) === "sqlserver" ? "rds-sqlserver" : "rds-mysql";
  // relational → split by PG/MySQL. Unknown relational → treat as PG group.
  return engineKind(engine) === "mysql" ? "aurora-mysql" : "aurora-postgresql";
}

export const ENGINE_GROUP_ORDER: EngineGroup[] = [
  "aurora-postgresql",
  "aurora-mysql",
  "rds-mysql",
  "rds-sqlserver",
  "documentdb",
  "dynamodb",
  "elasticache",
];

export interface EngineGroupMeta {
  label: string;
  accent: string;
  classes: string;
}
export const ENGINE_GROUP_META: Record<EngineGroup, EngineGroupMeta> = {
  "aurora-postgresql": {
    label: "Aurora PostgreSQL",
    accent: "bg-sky-400",
    classes: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  },
  "aurora-mysql": {
    label: "Aurora MySQL",
    accent: "bg-orange-400",
    classes: "bg-orange-500/15 text-orange-300 border-orange-500/40",
  },
  "rds-mysql": {
    label: "RDS MySQL",
    accent: "bg-amber-400",
    classes: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  },
  "rds-sqlserver": {
    label: "RDS SQL Server",
    accent: "bg-indigo-400",
    classes: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40",
  },
  documentdb: {
    label: "DocumentDB",
    accent: "bg-emerald-400",
    classes: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  },
  dynamodb: {
    label: "DynamoDB",
    accent: "bg-violet-400",
    classes: "bg-violet-500/15 text-violet-300 border-violet-500/40",
  },
  elasticache: {
    label: "ElastiCache",
    accent: "bg-rose-400",
    classes: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  },
};

// --- Engine families (multi-engine foundation) ---
// A family groups engines that share a monitoring/dashboard shape. `relational`
// = Aurora MySQL/PostgreSQL (SQL, RDS Data API, instances); `documentdb` =
// DocumentDB (MongoDB protocol, cluster/instance); `dynamodb` = DynamoDB
// (tables, capacity/throttle, no instances). Mirrors the backend
// engine_family.py / CAPABILITIES so panel gating agrees across the stack.
export type EngineFamily =
  | "relational"
  | "documentdb"
  | "dynamodb"
  | "elasticache"
  | "rds_instance";

export function engineFamily(engine: string | null | undefined): EngineFamily {
  const e = (engine || "").toLowerCase();
  if (e.includes("docdb") || e.includes("documentdb")) return "documentdb";
  if (e.includes("dynamodb")) return "dynamodb";
  if (
    e.includes("redis") ||
    e.includes("valkey") ||
    e.includes("memcached") ||
    e.includes("elasticache")
  )
    return "elasticache";
  // RDS instance engines (non-Aurora). 'aurora-mysql' contains 'mysql' — the
  // aurora guard keeps Aurora MySQL relational. Mirrors engine_family.py.
  if (e.includes("sqlserver")) return "rds_instance";
  if (e.includes("mysql") && !e.includes("aurora")) return "rds_instance";
  return "relational";
}

export interface FamilyMeta {
  label: string;
  noun: string; // unit-of-management noun for the family ("클러스터" / "테이블")
  accent: string; // tailwind bg for accent dots
  classes: string; // tailwind bg/text/border for badges
}

export const FAMILY_META: Record<EngineFamily, FamilyMeta> = {
  relational: {
    label: "Relational (Aurora)",
    noun: "클러스터",
    accent: "bg-sky-400",
    classes: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  },
  documentdb: {
    label: "DocumentDB",
    noun: "클러스터",
    accent: "bg-emerald-400",
    classes: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  },
  dynamodb: {
    label: "DynamoDB",
    noun: "테이블",
    accent: "bg-violet-400",
    classes: "bg-violet-500/15 text-violet-300 border-violet-500/40",
  },
  elasticache: {
    label: "ElastiCache",
    noun: "클러스터",
    accent: "bg-rose-400",
    classes: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  },
  rds_instance: {
    label: "RDS Instance",
    noun: "인스턴스",
    accent: "bg-indigo-400",
    classes: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40",
  },
};

// Which dashboard panels a family renders. The `relational` sentinel means
// "render the existing full Aurora panel set"; the new families enumerate their
// own panel keys (consumed by dashboard/page.tsx gating). Mirrors backend
// CAPABILITIES — keep in sync.
export const FAMILY_PANELS: Record<EngineFamily, Set<string>> = {
  relational: new Set(["all-relational"]),
  documentdb: new Set([
    "overview",
    "connections",
    "replicaLag",
    "cacheHit",
    "cursors",
    "opcounters",
    "backups",
  ]),
  dynamodb: new Set(["overview", "capacity", "throttles", "latency", "cost"]),
  elasticache: new Set([
    "overview",
    "memory",
    "hitRate",
    "connections",
    "evictions",
    "throughput",
    "replicationLag",
  ]),
  rds_instance: new Set(["overview"]),
};

// Query-path capability keys, mirroring backend CAPABILITIES (engine_family.py).
// query_stats rows are only ever written for relational (pg_stat_statements /
// events_statements_summary_by_digest) and rds_instance (direct-TCP collectors),
// so every other family would render a false empty state. explain and
// index_advice are PG-only implementations today (E-2 adds Aurora MySQL).
// cluster_parameter is Aurora cluster parameter groups; rds_instance has
// instance parameter groups instead (E-3). Keep in sync with the Python copies.
export type EngineCapability =
  | "query_stats"
  | "explain"
  | "index_advice"
  | "cluster_parameter";

export const FAMILY_CAPABILITIES: Record<
  EngineFamily,
  Record<EngineCapability, boolean>
> = {
  relational: {
    query_stats: true,
    explain: true,
    index_advice: true,
    cluster_parameter: true,
  },
  documentdb: {
    query_stats: false,
    explain: false,
    index_advice: false,
    cluster_parameter: false,
  },
  dynamodb: {
    query_stats: false,
    explain: false,
    index_advice: false,
    cluster_parameter: false,
  },
  elasticache: {
    query_stats: false,
    explain: false,
    index_advice: false,
    cluster_parameter: false,
  },
  rds_instance: {
    query_stats: true,
    explain: false,
    index_advice: false,
    cluster_parameter: false,
  },
};
