"use client";

import { useState, useCallback, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { QueryEditor } from "@/components/query-lab/query-editor";
import {
  PlanTree,
  summarizePlanForLLM,
} from "@/components/query-lab/plan-tree";
import { streamChat } from "@/lib/agentcore-sse";
import {
  fetchClusters,
  runExplain,
  ExplainSqlError,
  type ExplainResponse,
  type PgPlanRoot,
} from "@/lib/api-client";
import { PageHeader, PageBody } from "@/components/design-system/page-shell";

interface ClusterRow {
  cluster_id: string;
  engine?: string;
}

const PRESETS = [
  {
    label: "EXPLAIN ANALYZE",
    template: "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <your-query-here>;",
    prompt:
      "Run EXPLAIN (ANALYZE, BUFFERS) for this query, summarize the plan, identify the most expensive node, and suggest improvements.",
  },
  {
    label: "Index recommendation",
    template: "SELECT * FROM <your-table> WHERE <conditions>;",
    prompt:
      "Analyze this query, suggest indexes that would improve it, and explain the tradeoffs (write cost, storage, selectivity).",
  },
  {
    label: "Lock conflict diagnosis",
    template: "-- the query that was reported as blocked",
    prompt:
      "This query was reported as blocked. Diagnose likely lock contention sources and recommend mitigations.",
  },
  {
    label: "Rewrite for performance",
    template: "-- original SQL",
    prompt:
      "Rewrite this SQL to be more performant on PostgreSQL. Explain why each change helps, preserving exact semantics.",
  },
];

type Tab = "plan" | "analysis";
type LoadingKind = "explain" | "analyze" | "bulk" | null;

interface SavedPlan {
  id: string;
  sql: string;
  cluster_id: string;
  saved_at: number;
  engine: string;
  elapsed_ms: number;
  plan: ExplainResponse["plan"];
}

const PLAN_HISTORY_KEY = "dbops_query_lab_plans_v1";
const PLAN_HISTORY_LIMIT = 10;

function loadPlanHistory(): SavedPlan[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(PLAN_HISTORY_KEY);
    return raw ? (JSON.parse(raw) as SavedPlan[]) : [];
  } catch {
    return [];
  }
}

function savePlanHistory(next: SavedPlan[]): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(
      PLAN_HISTORY_KEY,
      JSON.stringify(next.slice(0, PLAN_HISTORY_LIMIT)),
    );
  } catch {
    // quota — drop oldest and retry once
    try {
      localStorage.setItem(PLAN_HISTORY_KEY, JSON.stringify(next.slice(0, 5)));
    } catch {
      // give up silently
    }
  }
}

function relTime(ms: number): string {
  const diff = Date.now() - ms;
  if (diff < 60_000) return "now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h`;
  return `${Math.floor(diff / 86_400_000)}d`;
}

export default function QueryLabPage() {
  const [clusters, setClusters] = useState<ClusterRow[]>([]);
  const [clusterId, setClusterId] = useState<string>("");
  const [analysis, setAnalysis] = useState("");
  const [explain, setExplain] = useState<ExplainResponse | null>(null);
  const [explainError, setExplainError] = useState<{
    message: string;
    kind: "sql" | "infra";
  } | null>(null);
  const [loadingKind, setLoadingKind] = useState<LoadingKind>(null);
  const [tab, setTab] = useState<Tab>("plan");
  const [presetPrompt, setPresetPrompt] = useState<string>("");
  // AI insight on the current plan (separate stream from the chat-driven
  // "AI 분석" tab — this one consumes the structured plan summary, not the
  // raw SQL).
  const [insight, setInsight] = useState<string>("");
  const [insightLoading, setInsightLoading] = useState(false);
  const [lastSql, setLastSql] = useState<string>("");
  const [history, setHistory] = useState<SavedPlan[]>([]);
  // Prefilled SQL — passed to QueryEditor on history-restore / share-link open.
  const [prefilledSql, setPrefilledSql] = useState<string>("");

  useEffect(() => {
    fetchClusters()
      .then((rows: ClusterRow[]) => {
        setClusters(rows);
        if (rows.length > 0) setClusterId(rows[0].cluster_id);
      })
      .catch(console.error);
  }, []);

  useEffect(() => {
    setHistory(loadPlanHistory());
  }, []);

  // Handle shared link: /query-lab#sql=<base64>&cluster=<id>
  useEffect(() => {
    if (typeof window === "undefined") return;
    const hash = window.location.hash.slice(1);
    if (!hash) return;
    const params = new URLSearchParams(hash);
    const sqlB64 = params.get("sql");
    const cid = params.get("cluster");
    if (sqlB64) {
      try {
        const sql = decodeURIComponent(escape(atob(sqlB64)));
        setPrefilledSql(sql);
      } catch {
        // ignore bad encoding
      }
    }
    if (cid) setClusterId(cid);
    // Clear the hash so refresh doesn't re-trigger.
    window.history.replaceState({}, "", window.location.pathname);
  }, []);

  const handleExplain = useCallback(
    async (sql: string) => {
      if (!clusterId) {
        setExplainError({ message: "Select a cluster first.", kind: "infra" });
        setTab("plan");
        return;
      }
      setExplainError(null);
      setExplain(null);
      setInsight("");
      setLastSql(sql);
      setLoadingKind("explain");
      setTab("plan");
      try {
        const res = await runExplain(clusterId, sql);
        setExplain(res);
        // Save to history (dedupe on identical sql+cluster — keep newest).
        setHistory((prev) => {
          const filtered = prev.filter(
            (h) => !(h.sql === sql && h.cluster_id === clusterId),
          );
          const entry: SavedPlan = {
            id: `plan-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
            sql,
            cluster_id: clusterId,
            saved_at: Date.now(),
            engine: res.engine,
            elapsed_ms: res.elapsed_ms,
            plan: res.plan,
          };
          const next = [entry, ...filtered].slice(0, PLAN_HISTORY_LIMIT);
          savePlanHistory(next);
          return next;
        });
      } catch (e) {
        if (e instanceof ExplainSqlError) {
          setExplainError({ message: e.message, kind: "sql" });
        } else {
          setExplainError({
            message: e instanceof Error ? e.message : "EXPLAIN failed",
            kind: "infra",
          });
        }
      } finally {
        setLoadingKind(null);
      }
    },
    [clusterId],
  );

  const handleGetInsight = useCallback(() => {
    if (!explain || !explain.plan) return;
    if (!clusterId) return;
    // PG-only for now — the summarizer assumes the PG shape.
    const arr = explain.plan as PgPlanRoot[];
    if (!Array.isArray(arr) || arr.length === 0 || !arr[0]?.Plan) {
      setInsight("AI insight is only available for PostgreSQL plans for now.");
      return;
    }
    const summary = summarizePlanForLLM(arr[0]);
    const sqlBlock = lastSql
      ? `\n\nSQL:\n\`\`\`sql\n${lastSql.trim()}\n\`\`\``
      : "";
    const message =
      `You are a Postgres performance expert. Below is a structured EXPLAIN ANALYZE summary. ` +
      `Identify the single biggest bottleneck and recommend 2–3 concrete fixes ` +
      `(indexes with column lists, query rewrites, schema changes, planner settings). ` +
      `Be specific — no generic advice. Keep the answer under 250 words.` +
      sqlBlock +
      `\n\nPlan summary:\n\`\`\`\n${summary}\n\`\`\``;
    setInsight("");
    setInsightLoading(true);
    streamChat(
      message,
      clusterId,
      (tok) => setInsight((prev) => prev + tok),
      () => {},
      () => setInsightLoading(false),
      (err) => {
        setInsight(`Error: ${err.message}`);
        setInsightLoading(false);
      },
    );
  }, [explain, clusterId, lastSql]);

  const handleBulkReview = useCallback(
    (sqlText: string) => {
      if (!clusterId) {
        setAnalysis("Select a cluster first.");
        setTab("analysis");
        return;
      }
      setAnalysis("");
      setLoadingKind("bulk");
      setTab("analysis");
      const message =
        `You are a senior DBA reviewing a batch of SQL statements before they hit production. ` +
        `The statements below are separated by semicolons (statements may contain inline ; inside strings; ` +
        `use SQL parsing judgment, not naive split).\n\n` +
        `For EACH statement, emit one row in a markdown table with columns:\n` +
        `| # | Statement (first 80 chars) | Risk | Notes |\n` +
        `Risk values: **safe** (read-only / parameter-bound DML), **risky** (large scan, missing where, ` +
        `lock-heavy), **dangerous** (DDL, DROP/TRUNCATE/DELETE without where, ALTER TABLE on hot table).\n` +
        `Notes should be 1 short sentence — specific to that query.\n` +
        `After the table, add a "Summary" section with the dangerous queries (if any) listed by index, ` +
        `and a single recommended next step.\n\n` +
        `Batch:\n\`\`\`sql\n${sqlText.trim()}\n\`\`\``;
      streamChat(
        message,
        clusterId,
        (token) => setAnalysis((prev) => prev + token),
        () => {},
        () => setLoadingKind(null),
        (err) => {
          setAnalysis(`Error: ${err.message}`);
          setLoadingKind(null);
        },
      );
    },
    [clusterId],
  );

  const handleAnalyze = useCallback(
    (sql: string) => {
      if (!clusterId) {
        setAnalysis("Select a cluster first.");
        setTab("analysis");
        return;
      }
      setAnalysis("");
      setLoadingKind("analyze");
      setTab("analysis");

      const intro =
        presetPrompt ||
        "Analyze this SQL for correctness, performance, and side effects. If a plan is requested, summarize it clearly.";
      const message = `${intro}\n\n\`\`\`sql\n${sql}\n\`\`\``;

      streamChat(
        message,
        clusterId,
        (token) => setAnalysis((prev) => prev + token),
        () => {},
        () => setLoadingKind(null),
        (err) => {
          setAnalysis(`Error: ${err.message}`);
          setLoadingKind(null);
        },
      );
    },
    [clusterId, presetPrompt],
  );

  const applyPreset = (template: string, prompt: string) => {
    setPresetPrompt(prompt);
    setAnalysis("");
    navigator.clipboard?.writeText(template).catch(() => {});
  };

  const hasPlan = explain && explain.plan;
  const hasAnalysis = analysis.length > 0;

  return (
    <PageBody>
      <PageHeader
        eyebrow="automate"
        title="Query Lab"
        description="EXPLAIN button renders a plan tree directly. AI 분석 sends the SQL to the agent for natural-language commentary."
        actions={
          <div className="flex items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">
              cluster
            </label>
            <select
              value={clusterId}
              onChange={(e) => setClusterId(e.target.value)}
              className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-1.5 focus:outline-none focus:border-amber-500/60"
            >
              {clusters.length === 0 && (
                <option value="">(no clusters registered)</option>
              )}
              {clusters.map((c) => (
                <option key={c.cluster_id} value={c.cluster_id}>
                  {c.cluster_id}
                </option>
              ))}
            </select>
          </div>
        }
      />

      <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-3 mb-4">
        <div className="text-[11px] text-zinc-500 uppercase tracking-wider mb-2">
          quick presets — copies a template to clipboard and primes the AI
          analysis prompt
        </div>
        <div className="flex flex-wrap gap-2 items-center">
          {PRESETS.map((p) => (
            <button
              key={p.label}
              onClick={() => applyPreset(p.template, p.prompt)}
              className="text-xs px-3 py-1.5 rounded border border-zinc-700 text-zinc-300 hover:border-sky-500 hover:text-sky-400 transition"
            >
              {p.label}
            </button>
          ))}
          {presetPrompt && (
            <button
              onClick={() => setPresetPrompt("")}
              className="text-xs px-3 py-1.5 rounded border border-zinc-700 text-zinc-500 hover:text-zinc-300 ml-auto"
            >
              clear preset
            </button>
          )}
        </div>
        {presetPrompt && (
          <div className="mt-2 text-[11px] text-sky-400 font-mono">
            prompt: {presetPrompt}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="space-y-4">
          <QueryEditor
            onExplain={handleExplain}
            onAnalyze={handleAnalyze}
            onBulkReview={handleBulkReview}
            isLoading={loadingKind !== null}
            loadingKind={loadingKind}
            initialSql={prefilledSql}
          />
          {history.length > 0 && (
            <div className="border border-zinc-800 bg-zinc-900/40">
              <div className="flex items-center justify-between px-3 py-2 border-b border-zinc-800">
                <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
                  recent plans · {history.length}
                </div>
                <button
                  onClick={() => {
                    setHistory([]);
                    savePlanHistory([]);
                  }}
                  className="text-[10px] text-zinc-500 hover:text-rose-400 transition-colors"
                >
                  clear all
                </button>
              </div>
              <div className="divide-y divide-zinc-800 max-h-[28rem] overflow-y-auto">
                {history.map((h) => (
                  <div
                    key={h.id}
                    className="px-3 py-2 hover:bg-zinc-800/40 group"
                  >
                    <div className="flex items-center gap-2 text-[10px] text-zinc-500 mb-1">
                      <span className="font-mono">
                        {h.engine.replace("aurora-", "")}
                      </span>
                      <span>·</span>
                      <span className="tabular-nums">{h.elapsed_ms}ms</span>
                      <span>·</span>
                      <span>{relTime(h.saved_at)}</span>
                      <span className="text-zinc-700">·</span>
                      <span className="truncate font-mono opacity-70">
                        {h.cluster_id}
                      </span>
                    </div>
                    <button
                      onClick={() => {
                        setPrefilledSql(h.sql);
                        if (h.cluster_id) setClusterId(h.cluster_id);
                        setExplain({
                          engine: h.engine,
                          cluster_id: h.cluster_id,
                          elapsed_ms: h.elapsed_ms,
                          sql: h.sql,
                          explain_sql: "",
                          plan: h.plan,
                          row_count: 0,
                        });
                        setLastSql(h.sql);
                        setInsight("");
                        setExplainError(null);
                        setTab("plan");
                      }}
                      className="block w-full text-left text-xs font-mono text-zinc-300 group-hover:text-zinc-100 truncate"
                      title={h.sql}
                    >
                      {h.sql.replace(/\s+/g, " ").slice(0, 120)}
                    </button>
                    <div className="flex items-center gap-3 mt-1.5 text-[10px]">
                      <button
                        onClick={async () => {
                          try {
                            await navigator.clipboard.writeText(
                              JSON.stringify(h.plan, null, 2),
                            );
                          } catch {
                            /* ignore */
                          }
                        }}
                        className="text-zinc-500 hover:text-sky-400 transition-colors"
                      >
                        copy json
                      </button>
                      <button
                        onClick={async () => {
                          const url = `${
                            window.location.origin
                          }/query-lab#sql=${btoa(
                            unescape(encodeURIComponent(h.sql)),
                          )}&cluster=${encodeURIComponent(h.cluster_id)}`;
                          try {
                            await navigator.clipboard.writeText(url);
                          } catch {
                            /* ignore */
                          }
                        }}
                        className="text-zinc-500 hover:text-sky-400 transition-colors"
                      >
                        copy link
                      </button>
                      <button
                        onClick={() => {
                          setHistory((prev) => {
                            const next = prev.filter((x) => x.id !== h.id);
                            savePlanHistory(next);
                            return next;
                          });
                        }}
                        className="ml-auto text-zinc-600 hover:text-rose-400 transition-colors"
                      >
                        remove
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
        <div className="bg-zinc-900/50 border border-zinc-800 p-4 overflow-y-auto max-h-[80vh]">
          <div className="flex items-center gap-2 mb-3">
            <button
              onClick={() => setTab("plan")}
              className={`text-[10px] uppercase tracking-[0.16em] px-2.5 py-1 border transition-colors ${
                tab === "plan"
                  ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
                  : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
              }`}
            >
              plan tree
            </button>
            <button
              onClick={() => setTab("analysis")}
              className={`text-[10px] uppercase tracking-[0.16em] px-2.5 py-1 border transition-colors ${
                tab === "analysis"
                  ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
                  : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
              }`}
            >
              ai analysis
            </button>
            {clusterId && (
              <>
                <span className="text-zinc-700">·</span>
                <span className="text-[10px] text-zinc-500 font-mono truncate">
                  {clusterId}
                </span>
              </>
            )}
            {tab === "plan" && explain && (
              <span className="ml-auto text-[10px] text-zinc-500 font-mono">
                {explain.engine.replace("aurora-", "")} · {explain.elapsed_ms}ms
              </span>
            )}
          </div>

          {tab === "plan" ? (
            <>
              {loadingKind === "explain" && (
                <div className="text-sm text-zinc-500">Running EXPLAIN…</div>
              )}
              {explainError && (
                <div
                  className={`text-xs px-3 py-2 mb-3 border ${
                    explainError.kind === "sql"
                      ? "text-amber-300 border-amber-500/40 bg-amber-500/10"
                      : "text-rose-400 border-rose-500/40 bg-rose-500/10"
                  }`}
                >
                  <div className="font-mono text-[10px] uppercase tracking-wider mb-1 opacity-70">
                    {explainError.kind === "sql"
                      ? "SQL error"
                      : "Execution failed"}
                  </div>
                  <div className="font-mono break-words">
                    {explainError.message}
                  </div>
                </div>
              )}
              {!loadingKind && !explainError && !hasPlan && (
                <div className="text-sm text-zinc-500">
                  Paste a SELECT into the editor and click{" "}
                  <span className="text-amber-300">EXPLAIN</span>.
                </div>
              )}
              {hasPlan && (
                <>
                  <PlanTree plan={explain!.plan} />
                  <div className="mt-4 border border-zinc-800 bg-zinc-900/40">
                    <div className="flex items-center justify-between px-3 py-2 border-b border-zinc-800">
                      <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
                        ai insight on this plan
                      </div>
                      <button
                        onClick={handleGetInsight}
                        disabled={insightLoading}
                        className="text-xs px-3 py-1 border border-sky-500/40 text-sky-300 hover:bg-sky-500/10 disabled:opacity-50 transition-colors"
                      >
                        {insightLoading
                          ? "thinking…"
                          : insight
                            ? "Re-analyze"
                            : "Get AI insight"}
                      </button>
                    </div>
                    <div className="p-3">
                      {!insight && !insightLoading && (
                        <div className="text-xs text-zinc-500">
                          Click{" "}
                          <span className="text-sky-300">Get AI insight</span>{" "}
                          for a 2–3 step recommendation tailored to this plan
                          (sends the structured summary, not the raw plan, so
                          the cost is small).
                        </div>
                      )}
                      {insight && (
                        <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-zinc-950 prose-pre:border prose-pre:border-zinc-800 prose-code:text-sky-300">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {insight}
                          </ReactMarkdown>
                        </div>
                      )}
                    </div>
                  </div>
                </>
              )}
            </>
          ) : (
            <>
              {hasAnalysis ? (
                <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-zinc-950 prose-pre:border prose-pre:border-zinc-800 prose-code:text-sky-300">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {analysis}
                  </ReactMarkdown>
                </div>
              ) : (
                <div className="text-sm text-zinc-500">
                  Pick a preset (template copied to clipboard), paste SQL in the
                  editor, then click
                  <span className="text-sky-400"> AI 분석</span>.
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </PageBody>
  );
}
