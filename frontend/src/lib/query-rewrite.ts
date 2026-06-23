// Pure helpers for the Query Lab rewrite feature.

// First fenced ```sql block (case-insensitive), trimmed; null if none.
export function extractSqlBlock(md: string): string | null {
  const m = md.match(/```sql\s*([\s\S]*?)```/i);
  const sql = m?.[1]?.trim();
  return sql ? sql : null;
}

// PG EXPLAIN FORMAT JSON: array[0].Plan["Total Cost"]. Returns null if not found.
export function planTotalCost(plan: unknown): number | null {
  const root = Array.isArray(plan) ? plan[0] : plan;
  const p = (root as { Plan?: { ["Total Cost"]?: number } } | undefined)?.Plan;
  const c = p?.["Total Cost"];
  return typeof c === "number" ? c : null;
}
