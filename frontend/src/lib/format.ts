/**
 * Shared display formatters. Panels previously rolled their own toFixed /
 * toLocaleString combinations, which produced inconsistent "12345678" rows
 * next to "12.3M" in another column. Use these everywhere a metric is
 * rendered for a human.
 */

const NUMBER_FORMATTER = new Intl.NumberFormat("en-US");

/**
 * Human-readable count: 1234 → "1.2k", 1_234_567 → "1.2M", 1_234_567_890 → "1.23B".
 * For small values (<1000) we keep the integer with a thousand separator so
 * the contrast against larger values is obvious.
 */
export function fmtNumber(v: number | string | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs < 1_000) return NUMBER_FORMATTER.format(Math.round(n * 100) / 100);
  if (abs < 1_000_000) return `${(n / 1_000).toFixed(abs < 10_000 ? 2 : 1)}k`;
  if (abs < 1_000_000_000)
    return `${(n / 1_000_000).toFixed(abs < 10_000_000 ? 2 : 1)}M`;
  if (abs < 1_000_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  return `${(n / 1_000_000_000_000).toFixed(2)}T`;
}

/**
 * Same as fmtNumber but always returns the precise count via en-US locale
 * grouping. Useful in tooltips / details panels where exact rows matter.
 */
export function fmtExact(v: number | string | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  return NUMBER_FORMATTER.format(n);
}

/** Bytes with binary-ish thresholds. Picks the largest unit that keeps the
 * value > 1. We use SI (1000) intentionally so it matches AWS/Postgres
 * conventions rather than IEC (1024). */
export function fmtBytes(v: number | string | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (abs >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (abs >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${Math.round(n)} B`;
}

/** Duration (ms). Picks micro/ms/s/min/hr automatically. */
export function fmtDuration(ms: number | string | null | undefined): string {
  if (ms === null || ms === undefined || ms === "") return "—";
  const n = typeof ms === "number" ? ms : Number(ms);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs < 1) return `${(n * 1000).toFixed(0)}µs`;
  if (abs < 1_000) return `${n.toFixed(abs < 10 ? 2 : abs < 100 ? 1 : 0)}ms`;
  if (abs < 60_000) return `${(n / 1_000).toFixed(abs < 10_000 ? 2 : 1)}s`;
  if (abs < 3_600_000) return `${(n / 60_000).toFixed(1)}min`;
  if (abs < 86_400_000) return `${(n / 3_600_000).toFixed(1)}hr`;
  return `${(n / 86_400_000).toFixed(1)}d`;
}

/** Percentage from a ratio (0..1) OR from a percentage value (0..100).
 * Pass `kind` to disambiguate. Default treats inputs > 1 as already-percent. */
export function fmtPct(
  v: number | string | null | undefined,
  kind: "ratio" | "percent" = "ratio",
  decimals = 0,
): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const asPct = kind === "ratio" ? n * 100 : n;
  return `${asPct.toFixed(decimals)}%`;
}

/** Choose a color class for a percentage. Higher = worse (red, amber, ok). */
export function pctTone(pct: number): string {
  if (pct > 80) return "text-rose-400";
  if (pct > 50) return "text-amber-400";
  return "text-zinc-300";
}

/** Hours/seconds since a timestamp ISO string. Used in panel "last refreshed". */
export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}
