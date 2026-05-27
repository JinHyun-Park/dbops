"use client";

import { useMemo, useState } from "react";
import type { PgPlanNode, PgPlanRoot } from "@/lib/api-client";

interface Props {
  plan: PgPlanRoot[] | Record<string, unknown> | null;
}

// PG's "Actual Total Time" is cumulative (this node + all children). Self
// time = cumulative - sum(child cumulative * child loops). We multiply child
// times by the child's loop count because PG reports per-loop times for
// nested-loop inner sides.
export function selfTime(node: PgPlanNode): number {
  const cumulative =
    (node["Actual Total Time"] ?? 0) * (node["Actual Loops"] ?? 1);
  const childTotal = (node.Plans ?? []).reduce(
    (acc, c) => acc + (c["Actual Total Time"] ?? 0) * (c["Actual Loops"] ?? 1),
    0,
  );
  return Math.max(0, cumulative - childTotal);
}

// Compact human-readable plan summary suitable for an LLM prompt. Keeps the
// shape (so the model can reason about join order / dependencies) but drops
// raw cost numbers we don't need at this level.
export function summarizePlanForLLM(root: PgPlanRoot): string {
  const total = totalSelfTime(root.Plan);
  const lines: string[] = [];
  lines.push(
    `Execution: ${root["Execution Time"]?.toFixed(2) ?? "?"}ms · Planning: ${
      root["Planning Time"]?.toFixed(2) ?? "?"
    }ms`,
  );

  // Hot nodes by self-time
  const ranked: {
    label: string;
    pct: number;
    ms: number;
    misest: number | null;
    details: string[];
  }[] = [];
  const walk = (n: PgPlanNode) => {
    const self = selfTime(n);
    const pct = total > 0 ? (self / total) * 100 : 0;
    const planRows = n["Plan Rows"];
    const actualRows = (n["Actual Rows"] ?? 0) * (n["Actual Loops"] ?? 1);
    const misest =
      planRows && planRows > 0 && actualRows > 0 ? actualRows / planRows : null;
    const details: string[] = [];
    for (const k of [
      "Filter",
      "Index Cond",
      "Hash Cond",
      "Join Filter",
      "Recheck Cond",
    ] as const) {
      const v = n[k as keyof PgPlanNode];
      if (v != null) details.push(`${k}: ${String(v)}`);
    }
    if (n["Shared Read Blocks"] && (n["Shared Read Blocks"] as number) > 100) {
      details.push(`Buffers: ${n["Shared Read Blocks"]} cold reads`);
    }
    ranked.push({ label: nodeLabel(n), pct, ms: self, misest, details });
    for (const c of n.Plans ?? []) walk(c);
  };
  walk(root.Plan);
  ranked.sort((a, b) => b.pct - a.pct);
  const top = ranked.slice(0, 5);
  lines.push("\nHot nodes (self-time):");
  for (const r of top) {
    const misest =
      r.misest != null && (r.misest > 10 || r.misest < 0.1)
        ? ` [planner ${
            r.misest > 1
              ? r.misest.toFixed(0) + "x under"
              : (1 / r.misest).toFixed(0) + "x over"
          }-estimate]`
        : "";
    lines.push(
      `  - ${r.label} — ${r.pct.toFixed(1)}% (${r.ms.toFixed(2)}ms)${misest}`,
    );
    for (const d of r.details) lines.push(`      ${d}`);
  }

  // Tree shape with indent
  lines.push("\nTree shape (indented; with relation / index / loops):");
  const walkTree = (n: PgPlanNode, depth: number) => {
    const rel = (n["Relation Name"] || n["Index Name"]) as string | undefined;
    const loops = n["Actual Loops"] ?? 1;
    const suffix = `${rel ? ` on ${rel}` : ""}${
      loops > 1 ? ` × ${loops} loops` : ""
    }`;
    lines.push(`${"  ".repeat(depth)}${n["Node Type"]}${suffix}`);
    for (const c of n.Plans ?? []) walkTree(c, depth + 1);
  };
  walkTree(root.Plan, 0);
  return lines.join("\n");
}

// Walk the tree and collect total self-time so we can render percentages.
function totalSelfTime(root: PgPlanNode): number {
  let total = selfTime(root);
  for (const c of root.Plans ?? []) total += totalSelfTime(c);
  return total;
}

function colorFor(pct: number): { ring: string; dot: string; text: string } {
  if (pct >= 50)
    return {
      ring: "border-rose-500/50",
      dot: "bg-rose-400",
      text: "text-rose-300",
    };
  if (pct >= 20)
    return {
      ring: "border-amber-500/50",
      dot: "bg-amber-400",
      text: "text-amber-300",
    };
  if (pct >= 5)
    return {
      ring: "border-zinc-700",
      dot: "bg-zinc-400",
      text: "text-zinc-300",
    };
  return { ring: "border-zinc-800", dot: "bg-zinc-600", text: "text-zinc-500" };
}

function nodeLabel(node: PgPlanNode): string {
  const t = node["Node Type"] || "?";
  const rel = (node["Relation Name"] ||
    node["Index Name"] ||
    node["CTE Name"] ||
    node["Subplan Name"]) as string | undefined;
  const alias = node["Alias"] as string | undefined;
  const strat = node["Strategy"] as string | undefined;
  const join = node["Join Type"] as string | undefined;
  const parts = [t];
  if (join) parts.push(join);
  if (strat && strat !== "Plain") parts.push(strat);
  if (rel) parts.push(`on ${rel}${alias && alias !== rel ? ` ${alias}` : ""}`);
  return parts.join(" ");
}

function fmtMs(ms: number | undefined): string {
  if (ms == null) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  if (ms >= 1) return `${ms.toFixed(2)}ms`;
  return `${(ms * 1000).toFixed(0)}µs`;
}

function fmtRows(n: number | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${n}`;
}

function NodeRow({
  node,
  depth,
  totalTime,
}: {
  node: PgPlanNode;
  depth: number;
  totalTime: number;
}) {
  const [open, setOpen] = useState(true);
  const [showDetail, setShowDetail] = useState(false);
  const self = selfTime(node);
  const pct = totalTime > 0 ? (self / totalTime) * 100 : 0;
  const c = colorFor(pct);
  const hasChildren = (node.Plans?.length ?? 0) > 0;

  // Misestimate ratio for the planner: actual_rows / plan_rows. Far from 1
  // means the optimizer's row guess was off, which is often the root cause
  // of a bad join order.
  const planRows = node["Plan Rows"];
  const actualRows = node["Actual Rows"];
  const actualLoops = node["Actual Loops"] ?? 1;
  const totalActualRows = (actualRows ?? 0) * actualLoops;
  const misest =
    planRows && planRows > 0 && totalActualRows > 0
      ? totalActualRows / planRows
      : null;
  const misestBad = misest !== null && (misest > 10 || misest < 0.1);

  const sharedHit = node["Shared Hit Blocks"] as number | undefined;
  const sharedRead = node["Shared Read Blocks"] as number | undefined;

  return (
    <div>
      <div
        className={`flex items-center gap-2 py-1.5 pr-3 border-l-2 ${c.ring} hover:bg-zinc-800/40 cursor-pointer transition-colors`}
        style={{ paddingLeft: `${0.5 + depth * 1.25}rem` }}
        onClick={() => setShowDetail((v) => !v)}
      >
        {hasChildren ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setOpen((v) => !v);
            }}
            className="text-zinc-500 hover:text-zinc-200 text-xs w-3 flex-shrink-0"
            aria-label={open ? "collapse" : "expand"}
          >
            {open ? "▼" : "▶"}
          </button>
        ) : (
          <span className="w-3 flex-shrink-0" />
        )}
        <span className={`w-2 h-2 rounded-full ${c.dot} flex-shrink-0`} />
        <span
          className={`text-sm font-medium ${c.text} truncate flex-1 min-w-0`}
        >
          {nodeLabel(node)}
        </span>
        <span className="text-[10px] text-zinc-600 tabular-nums flex-shrink-0">
          {pct >= 0.1 ? `${pct.toFixed(1)}%` : ""}
        </span>
        <span className="text-[10px] text-zinc-500 tabular-nums flex-shrink-0 w-16 text-right">
          {fmtMs(self)} self
        </span>
        <span className="text-[10px] text-zinc-500 tabular-nums flex-shrink-0 w-16 text-right">
          {fmtRows(totalActualRows)} rows
        </span>
        {misestBad && (
          <span
            title={`planner thought ${fmtRows(
              planRows ?? 0,
            )} rows, got ${fmtRows(totalActualRows)} (${misest!.toFixed(
              1,
            )}x off)`}
            className="text-[10px] font-mono px-1.5 py-0.5 border border-rose-500/40 text-rose-300 flex-shrink-0"
          >
            {misest! > 1
              ? `${misest!.toFixed(0)}x↑`
              : `${(1 / misest!).toFixed(0)}x↓`}
          </span>
        )}
      </div>

      {showDetail && (
        <div
          className="text-[11px] font-mono text-zinc-400 bg-zinc-950 border-l-2 border-zinc-800 py-2 pr-4 space-y-0.5"
          style={{ paddingLeft: `${1.5 + depth * 1.25}rem` }}
        >
          {(
            [
              "Filter",
              "Index Cond",
              "Hash Cond",
              "Recheck Cond",
              "Sort Key",
              "Group Key",
              "Join Filter",
              "Output",
            ] as const
          ).map((k) => {
            const v = node[k as keyof PgPlanNode];
            if (v == null) return null;
            const text = Array.isArray(v)
              ? (v as unknown[]).join(", ")
              : String(v);
            return (
              <div key={k}>
                <span className="text-zinc-600">{k}:</span>{" "}
                <span className="text-zinc-300">{text}</span>
              </div>
            );
          })}
          <div className="text-zinc-600 mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5">
            <div>
              cost:{" "}
              <span className="text-zinc-400">
                {(node["Startup Cost"] ?? 0).toFixed(2)}..
                {(node["Total Cost"] ?? 0).toFixed(2)}
              </span>
            </div>
            <div>
              loops: <span className="text-zinc-400">{actualLoops}</span>
            </div>
            <div>
              cumulative:{" "}
              <span className="text-zinc-400">
                {fmtMs((node["Actual Total Time"] ?? 0) * actualLoops)}
              </span>
            </div>
            <div>
              width:{" "}
              <span className="text-zinc-400">{node["Plan Width"] ?? "—"}</span>
            </div>
            {(sharedHit != null || sharedRead != null) && (
              <div className="col-span-2">
                buffers:{" "}
                <span className="text-zinc-400">
                  hit={sharedHit ?? 0} read={sharedRead ?? 0}
                  {sharedRead && sharedRead > 100 ? " ← cold reads" : ""}
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      {open &&
        hasChildren &&
        node.Plans!.map((child, i) => (
          <NodeRow
            key={i}
            node={child}
            depth={depth + 1}
            totalTime={totalTime}
          />
        ))}
    </div>
  );
}

// ----- Anti-pattern detection ----------------------------------------------
//
// These rules mirror what pgmustard / pganalyze surface as "issues" — they
// catch the patterns that explain plan readers learn to look for by hand.
// Each rule returns null (not a hit) or an Issue. We tune thresholds high
// enough that small-table noise (sample dbs, dev queries) doesn't dominate.
//
// Severity meaning:
//   critical → likely the cause of slowness
//   warning  → suspicious, worth investigating
//   info     → noteworthy but may be expected

interface Issue {
  severity: "critical" | "warning" | "info";
  title: string;
  detail: string;
  node: string;
  fix: string;
}

function detectIssues(root: PgPlanNode, totalTime: number): Issue[] {
  const issues: Issue[] = [];

  const walk = (n: PgPlanNode, parent: PgPlanNode | null) => {
    const label = nodeLabel(n);
    const self = selfTime(n);
    const pct = totalTime > 0 ? (self / totalTime) * 100 : 0;
    const loops = (n["Actual Loops"] ?? 1) as number;
    const actualRows = ((n["Actual Rows"] ?? 0) as number) * loops;
    const planRows = (n["Plan Rows"] ?? 0) as number;

    // 1. Slow Seq Scan — full table scan eating > 20% of execution
    if (n["Node Type"] === "Seq Scan" && pct > 20 && actualRows > 10_000) {
      issues.push({
        severity: pct > 40 ? "critical" : "warning",
        title: "Sequential scan on a large table",
        detail: `${label} read ${fmtRows(actualRows)} rows · ${pct.toFixed(
          1,
        )}% of execution time`,
        node: label,
        fix: "선택성 높은 컬럼에 인덱스 추가, 또는 WHERE 절을 인덱스로 cover 되게 재작성",
      });
    }

    // 2. Sort spilled to disk
    const sortMethod = n["Sort Method"] as string | undefined;
    if (
      sortMethod &&
      (sortMethod.toLowerCase().includes("external") ||
        sortMethod.toLowerCase().includes("disk"))
    ) {
      const spaceKb = n["Sort Space Used"] as number | undefined;
      issues.push({
        severity: "warning",
        title: "Sort spilled to disk",
        detail: `${label} · method "${sortMethod}"${
          spaceKb ? ` · ${fmtRows(spaceKb)} kB used` : ""
        }`,
        node: label,
        fix: "work_mem를 늘려 in-memory 정렬 유도 (세션 내 SET work_mem) · 또는 LIMIT을 더 빠른 단계로 push down",
      });
    }

    // 3. Hash join with multi-batch (spills to disk during build phase)
    const hashBatches = n["Hash Batches"] as number | undefined;
    if (hashBatches && hashBatches > 1) {
      issues.push({
        severity: "warning",
        title: "Hash multi-batch (disk spill)",
        detail: `${label} · ${hashBatches} batches`,
        node: label,
        fix: "work_mem 부족 — 빌드측 테이블이 hash table에 안 맞음. work_mem 증가 또는 join order 변경 검토",
      });
    }

    // 4. Lossy bitmap recheck — index scan re-read many rows after bitmap
    const rechecked = n["Rows Removed by Recheck"] as number | undefined;
    if (rechecked && rechecked > 10_000) {
      issues.push({
        severity: "info",
        title: "Bitmap recheck dropping many rows",
        detail: `${label} · ${fmtRows(rechecked)} rows discarded after bitmap`,
        node: label,
        fix: "work_mem 부족으로 lossy bitmap이 됨 — 해당 인덱스 selectivity 재확인 또는 work_mem 상향",
      });
    }

    // 5. Misestimate — planner badly wrong about row count
    if (planRows > 0 && actualRows > 0) {
      const ratio = actualRows / planRows;
      if (ratio > 100 || ratio < 0.01) {
        // Only flag at the level that actually drives downstream join choice
        // (parent is a join node) or at high-impact nodes (>10% self-time).
        const parentJoinish =
          parent &&
          ["Nested Loop", "Hash Join", "Merge Join"].includes(
            (parent["Node Type"] || "") as string,
          );
        if (parentJoinish || pct > 10) {
          issues.push({
            severity: ratio > 1000 || ratio < 0.001 ? "warning" : "info",
            title: "Row-count misestimate",
            detail: `${label} · planner expected ${fmtRows(
              planRows,
            )}, got ${fmtRows(actualRows)} (${
              ratio > 1
                ? `${ratio.toFixed(0)}x↑`
                : `${(1 / ratio).toFixed(0)}x↓`
            })`,
            node: label,
            fix: "ANALYZE 실행으로 통계 갱신 · default_statistics_target 상향 · CREATE STATISTICS로 다중 컬럼 의존성 통계 추가",
          });
        }
      }
    }

    // 6. Cold buffer reads — high disk reads relative to cache hits
    const sharedRead = n["Shared Read Blocks"] as number | undefined;
    const sharedHit = n["Shared Hit Blocks"] as number | undefined;
    if (sharedRead && sharedRead > 1000) {
      const ratio =
        sharedHit != null ? sharedRead / (sharedHit + sharedRead) : 1;
      if (ratio > 0.3) {
        issues.push({
          severity: "info",
          title: "Cold buffer reads",
          detail: `${label} · ${sharedRead} disk reads vs ${
            sharedHit ?? 0
          } cache hits (${(ratio * 100).toFixed(0)}% miss)`,
          node: label,
          fix: "shared_buffers 또는 메모리 부족 · 쿼리가 처음 실행이면 두번째부터 캐시됨. 반복 실행에도 cold면 working set이 buffer를 초과",
        });
      }
    }

    // 7. Nested Loop with high inner-loop count — n*m blow-up
    if (n["Node Type"] === "Nested Loop" && (n.Plans?.length ?? 0) >= 2) {
      const inner = n.Plans![1];
      const innerLoops = (inner["Actual Loops"] ?? 0) as number;
      if (innerLoops > 5000) {
        issues.push({
          severity: pct > 30 ? "critical" : "warning",
          title: "Nested loop with high inner repetition",
          detail: `${label} · inner side ran ${fmtRows(innerLoops)} times`,
          node: label,
          fix: "Hash Join 또는 Merge Join을 유도 (조인 컬럼에 인덱스 + ANALYZE) · 또는 SET enable_nestloop=off로 검증",
        });
      }
    }

    for (const c of n.Plans ?? []) walk(c, n);
  };

  walk(root, null);

  // Stable sort by severity then by detail string so the panel doesn't
  // shuffle between renders if the underlying plan rewalk produces an
  // equivalent set.
  const order = { critical: 0, warning: 1, info: 2 };
  return issues.sort((a, b) => {
    if (order[a.severity] !== order[b.severity])
      return order[a.severity] - order[b.severity];
    return a.title.localeCompare(b.title);
  });
}

function IssuesPanel({
  root,
  totalTime,
}: {
  root: PgPlanNode;
  totalTime: number;
}) {
  const issues = useMemo(
    () => detectIssues(root, totalTime),
    [root, totalTime],
  );
  if (issues.length === 0) return null;

  const counts = {
    critical: issues.filter((i) => i.severity === "critical").length,
    warning: issues.filter((i) => i.severity === "warning").length,
    info: issues.filter((i) => i.severity === "info").length,
  };

  return (
    <div className="border border-zinc-800 bg-zinc-900/40 mb-3">
      <div className="flex items-center gap-3 px-3 py-2 border-b border-zinc-800">
        <span className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
          issues found
        </span>
        {counts.critical > 0 && (
          <span className="text-[10px] px-1.5 py-0.5 border border-rose-500/40 bg-rose-500/10 text-rose-300 font-mono">
            {counts.critical} critical
          </span>
        )}
        {counts.warning > 0 && (
          <span className="text-[10px] px-1.5 py-0.5 border border-amber-500/40 bg-amber-500/10 text-amber-300 font-mono">
            {counts.warning} warning
          </span>
        )}
        {counts.info > 0 && (
          <span className="text-[10px] px-1.5 py-0.5 border border-zinc-700 bg-zinc-800/40 text-zinc-400 font-mono">
            {counts.info} info
          </span>
        )}
      </div>
      <ul className="divide-y divide-zinc-800/60">
        {issues.map((iss, i) => {
          const tone =
            iss.severity === "critical"
              ? {
                  bar: "border-l-rose-500",
                  title: "text-rose-300",
                  dot: "bg-rose-400",
                }
              : iss.severity === "warning"
                ? {
                    bar: "border-l-amber-500",
                    title: "text-amber-300",
                    dot: "bg-amber-400",
                  }
                : {
                    bar: "border-l-zinc-600",
                    title: "text-zinc-300",
                    dot: "bg-zinc-500",
                  };
          return (
            <li
              key={i}
              className={`px-3 py-2 border-l-2 ${tone.bar} hover:bg-zinc-800/30 transition-colors`}
            >
              <div className="flex items-baseline gap-2">
                <span
                  className={`w-1.5 h-1.5 rounded-full ${tone.dot} flex-shrink-0 mt-0.5`}
                />
                <span className={`text-xs font-medium ${tone.title}`}>
                  {iss.title}
                </span>
              </div>
              <div className="text-[11px] text-zinc-400 mt-1 ml-3.5 font-mono">
                {iss.detail}
              </div>
              <div className="text-[11px] text-zinc-500 mt-1 ml-3.5">
                <span className="text-zinc-600">→ </span>
                {iss.fix}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function HotNodes({
  root,
  totalTime,
}: {
  root: PgPlanNode;
  totalTime: number;
}) {
  const ranked = useMemo(() => {
    const out: {
      label: string;
      pct: number;
      ms: number;
      misest: number | null;
    }[] = [];
    const walk = (n: PgPlanNode) => {
      const self = selfTime(n);
      const pct = totalTime > 0 ? (self / totalTime) * 100 : 0;
      const planRows = n["Plan Rows"];
      const actualRows = (n["Actual Rows"] ?? 0) * (n["Actual Loops"] ?? 1);
      const misest =
        planRows && planRows > 0 && actualRows > 0
          ? actualRows / planRows
          : null;
      out.push({ label: nodeLabel(n), pct, ms: self, misest });
      for (const c of n.Plans ?? []) walk(c);
    };
    walk(root);
    return out.sort((a, b) => b.pct - a.pct).slice(0, 3);
  }, [root, totalTime]);

  if (ranked.length === 0 || ranked[0].pct < 1) return null;

  return (
    <div className="border border-zinc-800 bg-zinc-900/40 p-3 mb-3">
      <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 mb-2">
        hot nodes
      </div>
      <div className="space-y-1.5">
        {ranked.map((r, i) => {
          const c = colorFor(r.pct);
          return (
            <div key={i} className="flex items-center gap-2 text-xs">
              <span className={`w-1.5 h-1.5 rounded-full ${c.dot}`} />
              <span className={`${c.text} flex-1 truncate`}>{r.label}</span>
              <span className="text-zinc-500 tabular-nums">
                {r.pct.toFixed(1)}%
              </span>
              <span className="text-zinc-600 tabular-nums w-16 text-right">
                {fmtMs(r.ms)}
              </span>
              {r.misest != null && (r.misest > 10 || r.misest < 0.1) && (
                <span className="text-rose-400 text-[10px] tabular-nums w-12 text-right">
                  {r.misest > 1
                    ? `${r.misest.toFixed(0)}x↑`
                    : `${(1 / r.misest).toFixed(0)}x↓`}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function PlanTree({ plan }: Props) {
  if (!plan) {
    return (
      <div className="text-sm text-zinc-500">
        No plan available. Run EXPLAIN to see the tree.
      </div>
    );
  }

  // PG returns a top-level array of one element.
  const isPgPlan =
    Array.isArray(plan) &&
    plan.length > 0 &&
    typeof plan[0] === "object" &&
    plan[0] !== null &&
    "Plan" in (plan[0] as object);

  if (!isPgPlan) {
    return (
      <div className="border border-zinc-800 bg-zinc-900/40 p-4">
        <div className="text-xs text-zinc-500 mb-2">
          Non-PostgreSQL plan — tree view not yet implemented for this engine.
        </div>
        <pre className="text-[11px] font-mono text-zinc-400 overflow-auto max-h-96 whitespace-pre-wrap">
          {JSON.stringify(plan, null, 2)}
        </pre>
      </div>
    );
  }

  const root = (plan as PgPlanRoot[])[0];
  const totalTime = totalSelfTime(root.Plan);
  const planningMs = root["Planning Time"] ?? 0;
  const execMs = root["Execution Time"] ?? 0;

  return (
    <div>
      <div className="grid grid-cols-3 gap-3 mb-3">
        <div className="border border-zinc-800 bg-zinc-900/40 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">
            execution
          </div>
          <div className="text-base text-zinc-100 mt-0.5 tabular-nums">
            {fmtMs(execMs)}
          </div>
        </div>
        <div className="border border-zinc-800 bg-zinc-900/40 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">
            planning
          </div>
          <div className="text-base text-zinc-100 mt-0.5 tabular-nums">
            {fmtMs(planningMs)}
          </div>
        </div>
        <div className="border border-zinc-800 bg-zinc-900/40 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">
            root node
          </div>
          <div className="text-base text-zinc-100 mt-0.5 truncate">
            {nodeLabel(root.Plan)}
          </div>
        </div>
      </div>

      <IssuesPanel root={root.Plan} totalTime={totalTime} />

      <HotNodes root={root.Plan} totalTime={totalTime} />

      <div className="border border-zinc-800 bg-zinc-900/40">
        <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 px-3 py-2 border-b border-zinc-800">
          plan tree · click a row for detail
        </div>
        <div className="overflow-y-auto max-h-[60vh]">
          <NodeRow node={root.Plan} depth={0} totalTime={totalTime} />
        </div>
      </div>
    </div>
  );
}
