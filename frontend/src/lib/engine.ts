// Engine helpers + Aurora version EOL schedule.
//
// AWS engine strings observed in the wild: "aurora-postgresql", "aurora-mysql".
// Treat anything else conservatively (UNKNOWN) so we don't accidentally hide
// PG-only panels on a future engine variant.

export type EngineKind = "postgres" | "mysql" | "unknown";

export function engineKind(engine: string | null | undefined): EngineKind {
  const e = (engine || "").toLowerCase();
  if (e.includes("postgres")) return "postgres";
  if (e.includes("mysql")) return "mysql";
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
  accent: string;  // for accent dots / pills
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
  { engine: "postgres", version_prefix: "11.", display_name: "PostgreSQL 11", eol: "2024-02-29", note: "Extended Support charges apply" },
  { engine: "postgres", version_prefix: "12.", display_name: "PostgreSQL 12", eol: "2025-02-28", note: "Extended Support charges apply" },
  { engine: "postgres", version_prefix: "13.", display_name: "PostgreSQL 13", eol: "2026-02-28" },
  { engine: "postgres", version_prefix: "14.", display_name: "PostgreSQL 14", eol: "2026-11-12" },
  { engine: "postgres", version_prefix: "15.", display_name: "PostgreSQL 15", eol: "2027-11-11" },
  { engine: "postgres", version_prefix: "16.", display_name: "PostgreSQL 16", eol: "2028-11-09" },
  { engine: "postgres", version_prefix: "17.", display_name: "PostgreSQL 17", eol: "2029-11-08" },

  // --- Aurora MySQL — version strings look like "5.7.mysql_aurora.2.x" or "8.0.mysql_aurora.3.x" ---
  { engine: "mysql", version_prefix: "5.7", display_name: "MySQL 5.7 (Aurora v2)", eol: "2024-10-31", note: "Aurora MySQL v2 — Extended Support active" },
  { engine: "mysql", version_prefix: "8.0", display_name: "MySQL 8.0 (Aurora v3)", eol: "2027-04-30" },
  { engine: "mysql", version_prefix: "8.4", display_name: "MySQL 8.4 (Aurora v4)", eol: "2032-04-30" },
];

export interface EolInfo {
  display_name: string;
  eol: string;
  days_remaining: number;
  status: "expired" | "imminent" | "soon" | "safe";
  note?: string;
}

export function eolFor(engine: string | null | undefined, version: string | null | undefined, now: Date = new Date()): EolInfo | null {
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
  const days = Math.floor((eolDate.getTime() - now.getTime()) / (24 * 3600 * 1000));
  let status: EolInfo["status"];
  if (days < 0) status = "expired";
  else if (days < 90) status = "imminent";
  else if (days < 365) status = "soon";
  else status = "safe";
  return { display_name: match.display_name, eol: match.eol, days_remaining: days, status, note: match.note };
}

export const EOL_STATUS_CLASSES: Record<EolInfo["status"], string> = {
  expired: "text-rose-400",
  imminent: "text-rose-400",
  soon: "text-amber-400",
  safe: "text-emerald-400",
};

export function eolHint(info: EolInfo): string {
  if (info.status === "expired") {
    return `${info.display_name} reached EOL ${Math.abs(info.days_remaining)} days ago (${info.eol})${info.note ? " — " + info.note : ""}`;
  }
  return `${info.display_name} EOL on ${info.eol} (${info.days_remaining} days remaining)${info.note ? " — " + info.note : ""}`;
}
