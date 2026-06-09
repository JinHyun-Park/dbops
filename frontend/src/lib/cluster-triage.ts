// Shared per-cluster operational triage. The Fleet table, the Fleet summary
// band, and the Dashboard cluster cards all derive severity from THIS one
// function so a cluster never reads "critical" in one place and "healthy" in
// another. Severity blends live operating signals (CPU / load / deadlocks /
// blocking) with RDS lifecycle status and engine EOL — a missing metric never
// counts against a cluster (null != a problem).
import { type EolInfo } from "@/lib/engine";

export type Level = "critical" | "warning" | "ok";

// The minimal shape triage needs. Fleet's richer ClusterRow satisfies this
// structurally; the Dashboard merges the same fields from the multi-cluster
// overview endpoint.
export interface TriageInput {
  status?: string | null;
  cpu?: number | string | null;
  aas?: number | string | null;
  deadlocks?: number | string | null;
  blocking_count?: number | string | null;
}

export interface TriageResult {
  level: Level;
  heat: number; // tiebreak within a level: higher = worse
  reasons: string[]; // why it's at this level (tooltip)
}

export function n(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

// Per-cluster triage from a single set of thresholds, so the summary band, the
// row dot, the cell colors, and the dashboard card pill all agree.
export function triage(c: TriageInput, eol: EolInfo | null): TriageResult {
  const cpu = c.cpu === null || c.cpu === undefined ? null : n(c.cpu);
  const aas = c.aas === null || c.aas === undefined ? null : n(c.aas);
  const dlk = n(c.deadlocks);
  const blk = n(c.blocking_count);
  let level: Level = "ok";

  const crit: string[] = [];
  if (c.status && c.status !== "available") crit.push(`status=${c.status}`);
  if (cpu !== null && cpu >= 90) crit.push(`CPU ${cpu.toFixed(0)}%`);
  if (aas !== null && aas >= 5) crit.push(`AAS ${aas.toFixed(1)}`);
  if (dlk >= 5) crit.push(`${dlk} deadlocks`);
  if (blk >= 3) crit.push(`${blk} blocking`);
  if (eol && (eol.status === "expired" || eol.status === "imminent"))
    crit.push(eol.status === "expired" ? "EOL passed" : "EOL imminent");

  const warn: string[] = [];
  if (cpu !== null && cpu >= 70) warn.push(`CPU ${cpu.toFixed(0)}%`);
  if (aas !== null && aas >= 2) warn.push(`AAS ${aas.toFixed(1)}`);
  if (dlk >= 1) warn.push(`${dlk} deadlocks`);
  if (blk >= 1) warn.push(`${blk} blocking`);
  if (eol && eol.status === "soon") warn.push("EOL < 1y");

  const reasons: string[] = [];
  if (crit.length) {
    level = "critical";
    reasons.push(...crit);
  } else if (warn.length) {
    level = "warning";
    reasons.push(...warn);
  }

  // Heat orders rows within a level: weight the scarier signals higher.
  const heat =
    (cpu ?? 0) +
    (aas ?? 0) * 15 +
    dlk * 8 +
    blk * 12 +
    (c.status && c.status !== "available" ? 100 : 0) +
    (eol?.status === "expired" ? 60 : eol?.status === "imminent" ? 30 : 0);

  return { level, heat, reasons };
}

export const LEVEL_RANK: Record<Level, number> = {
  critical: 2,
  warning: 1,
  ok: 0,
};
