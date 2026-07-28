"use client";

import { useMemo, useState } from "react";
import type {
  MysqlPlanRoot,
  MysqlTableNode,
  PgPlanNode,
  PgPlanRoot,
} from "@/lib/api-client";

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
        title: "대형 테이블 Seq Scan",
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
        title: "정렬이 디스크로 스필됨",
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
        title: "Hash 멀티배치 (디스크 스필)",
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
        title: "Bitmap recheck에서 다량 행 폐기",
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
            title: "행 수 추정 오차",
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
          title: "콜드 버퍼 읽기 (캐시 미스 높음)",
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
          title: "Nested Loop 내부 반복 과다",
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

// ---------------------------------------------------------------------------
// MySQL (Aurora MySQL) — EXPLAIN FORMAT=JSON
//
// Mirrors mcp_servers/performance/tools/explain_plan.py._walk_mysql. MySQL's plan
// is a flat, ordered list of table accesses (the join order), so it is rendered
// as a list rather than forced into a tree it does not have. Recursion beats
// enumerating wrapper keys: ordering_operation / grouping_operation /
// duplicates_removal / materialized_from_subquery / union_result all nest, and a
// missed wrapper would silently drop that subtree.
// ---------------------------------------------------------------------------

export function isMysqlPlan(
  plan: PgPlanRoot[] | Record<string, unknown> | null,
): plan is MysqlPlanRoot {
  return (
    !!plan &&
    !Array.isArray(plan) &&
    typeof (plan as Record<string, unknown>).query_block === "object" &&
    (plan as Record<string, unknown>).query_block !== null
  );
}

type MysqlFlags = { filesort: boolean; temporaryTable: boolean };

function walkMysql(
  node: unknown,
  tables: MysqlTableNode[],
  flags: MysqlFlags,
): void {
  if (Array.isArray(node)) {
    for (const v of node) walkMysql(v, tables, flags);
    return;
  }
  if (typeof node !== "object" || node === null) return;
  const obj = node as Record<string, unknown>;
  if ("table_name" in obj) tables.push(obj as MysqlTableNode);
  if (obj.using_filesort === true) flags.filesort = true;
  if (obj.using_temporary_table === true) flags.temporaryTable = true;
  for (const v of Object.values(obj)) walkMysql(v, tables, flags);
}

function mysqlPlanParts(plan: MysqlPlanRoot) {
  const tables: MysqlTableNode[] = [];
  const flags: MysqlFlags = { filesort: false, temporaryTable: false };
  walkMysql(plan.query_block, tables, flags);
  const queryCost = Number(plan.query_block.cost_info?.query_cost);
  return {
    tables,
    flags,
    queryCost: Number.isFinite(queryCost) ? queryCost : null,
  };
}

// Same thresholds as the MCP tool, so the panel and the agent agree.
const MYSQL_BIG_SCAN_ROWS = 10_000;
const MYSQL_LOW_FILTERED_PCT = 50;

function fmtCost(n: number | null): string {
  if (n == null) return "—";
  return n >= 1000 ? Math.round(n).toLocaleString() : n.toFixed(2);
}

// Compact MySQL plan summary for an LLM prompt. Deliberately states which
// analyses the plan CANNOT support, so the model does not invent them: every row
// count here is an estimate, and MySQL's plan-only EXPLAIN has no timings.
export function summarizeMysqlPlanForLLM(plan: MysqlPlanRoot): string {
  const { tables, flags, queryCost } = mysqlPlanParts(plan);
  const lines: string[] = [];
  lines.push(
    `Engine: MySQL · EXPLAIN FORMAT=JSON (plan only, NOT executed) · query_cost: ${fmtCost(
      queryCost,
    )}`,
  );
  lines.push(
    "All row counts below are OPTIMIZER ESTIMATES. There are no actual row counts, no timings and no buffer stats: MySQL's EXPLAIN ANALYZE does not emit JSON, so estimate-vs-actual and disk-spill analysis are unavailable for this plan. Do not claim either.",
  );
  lines.push("\nJoin order (each step reads the row source named):");
  tables.forEach((t, i) => {
    const bits = [
      `access_type=${t.access_type ?? "?"}`,
      `rows_examined_per_scan=${t.rows_examined_per_scan ?? "?"}`,
      `rows_produced_per_join=${t.rows_produced_per_join ?? "?"}`,
      `filtered=${t.filtered ?? "?"}%`,
    ];
    if (t.key) bits.push(`key=${t.key}`);
    else if (t.possible_keys?.length)
      bits.push(`possible_keys=${t.possible_keys.join(",")} (none chosen)`);
    else bits.push("no index used");
    if (t.cost_info?.prefix_cost)
      bits.push(`prefix_cost=${t.cost_info.prefix_cost}`);
    lines.push(`  ${i + 1}. ${t.table_name ?? "?"} — ${bits.join(" · ")}`);
    if (t.attached_condition)
      lines.push(`       condition: ${t.attached_condition}`);
  });
  const strategy: string[] = [];
  if (flags.filesort)
    strategy.push("using_filesort=true (sort not served by an index)");
  if (flags.temporaryTable)
    strategy.push("using_temporary_table=true (internal temp table)");
  if (strategy.length)
    lines.push("\nOptimizer strategy:\n  - " + strategy.join("\n  - "));
  return lines.join("\n");
}

function MysqlIssues({
  tables,
  flags,
  queryCost,
}: {
  tables: MysqlTableNode[];
  flags: MysqlFlags;
  queryCost: number | null;
}) {
  const issues: { severity: "high" | "medium" | "info"; text: string }[] = [];
  for (const t of tables) {
    const examined = t.rows_examined_per_scan ?? 0;
    const filtered = Number(t.filtered);
    if (t.access_type === "ALL" && examined >= MYSQL_BIG_SCAN_ROWS) {
      issues.push({
        severity: "high",
        text: `${t.table_name}: access_type=ALL, ${fmtRows(
          examined,
        )} 행을 전부 훑습니다. WHERE·JOIN 컬럼에 인덱스를 검토하세요.`,
      });
    }
    if (
      Number.isFinite(filtered) &&
      filtered < MYSQL_LOW_FILTERED_PCT &&
      examined >= MYSQL_BIG_SCAN_ROWS
    ) {
      issues.push({
        severity: "medium",
        text: `${t.table_name}: ${fmtRows(examined)} 행을 읽어 약 ${fmtRows(
          t.rows_produced_per_join,
        )} 행만 남깁니다 (filtered ${filtered}%). 더 선택적인 인덱스가 읽는 행 수를 줄입니다.`,
      });
    }
  }
  if (flags.filesort) {
    issues.push({
      severity: "medium",
      text: "using_filesort: 정렬을 인덱스 순서로 처리하지 못해 옵티마이저가 직접 정렬합니다. ORDER BY·GROUP BY 컬럼에 맞는 인덱스로 정렬을 없앨 수 있습니다.",
    });
  }
  if (flags.temporaryTable) {
    issues.push({
      severity: "medium",
      text: "using_temporary_table: 내부 임시 테이블을 만듭니다 (인덱스로 해결되지 않는 GROUP BY·DISTINCT·UNION에서 흔합니다). 커지면 디스크로 스필합니다.",
    });
  }
  if (queryCost != null && queryCost >= 100_000) {
    issues.push({
      severity: "info",
      text: `query_cost ${fmtCost(queryCost)} — 전체적으로 비싼 플랜입니다.`,
    });
  }
  if (issues.length === 0) return null;

  const tone = {
    high: "text-rose-300",
    medium: "text-amber-300",
    info: "text-sky-300",
  } as const;
  const dot = {
    high: "bg-rose-400",
    medium: "bg-amber-400",
    info: "bg-sky-400",
  } as const;
  return (
    <div className="border border-zinc-800 bg-zinc-900/40 p-3 mb-3">
      <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 mb-2">
        plan signals
      </div>
      <ul className="space-y-1.5">
        {issues.map((iss, i) => (
          <li key={i} className="flex items-start gap-2 text-xs">
            <span
              className={`w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 ${
                dot[iss.severity]
              }`}
            />
            <span className={tone[iss.severity]}>{iss.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function MysqlPlanView({ plan }: { plan: MysqlPlanRoot }) {
  const { tables, flags, queryCost } = useMemo(
    () => mysqlPlanParts(plan),
    [plan],
  );
  const strategy =
    [flags.filesort && "filesort", flags.temporaryTable && "temp table"]
      .filter(Boolean)
      .join(" + ") || "none";

  return (
    <div>
      <div className="grid grid-cols-3 gap-3 mb-3">
        <div className="border border-zinc-800 bg-zinc-900/40 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">
            query cost
          </div>
          <div className="text-base text-zinc-100 mt-0.5 tabular-nums">
            {fmtCost(queryCost)}
          </div>
        </div>
        <div className="border border-zinc-800 bg-zinc-900/40 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">
            row sources
          </div>
          <div className="text-base text-zinc-100 mt-0.5 tabular-nums">
            {tables.length}
          </div>
        </div>
        <div className="border border-zinc-800 bg-zinc-900/40 px-3 py-2">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">
            strategy
          </div>
          <div className="text-base text-zinc-100 mt-0.5 truncate">
            {strategy}
          </div>
        </div>
      </div>

      <MysqlIssues tables={tables} flags={flags} queryCost={queryCost} />

      <div className="border border-zinc-800 bg-zinc-900/40">
        <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 px-3 py-2 border-b border-zinc-800">
          join order · 추정값입니다 (실행 통계 아님)
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-zinc-500 border-b border-zinc-800">
                <th className="text-left font-normal px-3 py-1.5">#</th>
                <th className="text-left font-normal px-3 py-1.5">table</th>
                <th className="text-left font-normal px-3 py-1.5">access</th>
                <th className="text-left font-normal px-3 py-1.5">key</th>
                <th className="text-right font-normal px-3 py-1.5">examined</th>
                <th className="text-right font-normal px-3 py-1.5">produced</th>
                <th className="text-right font-normal px-3 py-1.5">filtered</th>
                <th className="text-right font-normal px-3 py-1.5">
                  prefix cost
                </th>
              </tr>
            </thead>
            <tbody>
              {tables.map((t, i) => {
                const full = t.access_type === "ALL";
                return (
                  <tr
                    key={i}
                    className={`border-b border-zinc-800/60 last:border-0 ${
                      full ? "text-rose-300" : "text-zinc-300"
                    }`}
                  >
                    <td className="px-3 py-1.5 text-zinc-500 tabular-nums">
                      {i + 1}
                    </td>
                    <td className="px-3 py-1.5 font-mono">
                      {t.table_name ?? "—"}
                    </td>
                    <td className="px-3 py-1.5 font-mono">
                      {t.access_type ?? "—"}
                    </td>
                    <td className="px-3 py-1.5 font-mono text-zinc-400">
                      {t.key ?? "—"}
                    </td>
                    <td className="px-3 py-1.5 text-right tabular-nums">
                      {fmtRows(t.rows_examined_per_scan)}
                    </td>
                    <td className="px-3 py-1.5 text-right tabular-nums text-zinc-400">
                      {fmtRows(t.rows_produced_per_join)}
                    </td>
                    <td className="px-3 py-1.5 text-right tabular-nums text-zinc-400">
                      {t.filtered != null ? `${t.filtered}%` : "—"}
                    </td>
                    <td className="px-3 py-1.5 text-right tabular-nums text-zinc-400">
                      {fmtCost(Number(t.cost_info?.prefix_cost) || null)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="text-[11px] text-zinc-500 mt-2 leading-relaxed">
        MySQL의 plan-only EXPLAIN에는 옵티마이저 소요 시간과 실제 행 수가 없어,
        추정 대비 실제 괴리(통계 부정확)와 디스크 스필 여부는 이 플랜으로 알 수
        없습니다. EXPLAIN ANALYZE는 JSON을 내주지 않습니다.
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
    if (isMysqlPlan(plan)) return <MysqlPlanView plan={plan} />;
    return (
      <div className="border border-zinc-800 bg-zinc-900/40 p-4">
        <div className="text-xs text-zinc-500 mb-2">
          이 엔진의 플랜 형식은 아직 구조화 렌더링을 지원하지 않습니다. 원본
          응답을 그대로 표시합니다.
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
