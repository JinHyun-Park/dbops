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
  runExplain,
  ExplainSqlError,
  type ExplainResponse,
  type PgPlanRoot,
  listSavedQueries,
  fetchSavedQuery,
  createSavedQuery,
  deleteSavedQuery,
  type SavedQuerySummary,
} from "@/lib/api-client";
import { PageHeader, PageBody } from "@/components/design-system/page-shell";
import { useSelectedCluster } from "@/lib/use-selected-cluster";
import { ClusterPicker } from "@/components/design-system/cluster-picker";

const PRESETS = [
  {
    label: "EXPLAIN ANALYZE",
    template: "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <your-query-here>;",
    prompt:
      "**한국어로** 답변해줘. 이 쿼리에 EXPLAIN (ANALYZE, BUFFERS)를 실행하고, plan을 요약 + 가장 비싼 노드 식별 + 개선안을 제시해줘.",
  },
  {
    label: "인덱스 추천",
    template: "SELECT * FROM <your-table> WHERE <conditions>;",
    prompt:
      "**한국어로** 답변해줘. 이 쿼리를 분석해서 개선할 수 있는 인덱스를 제안하고, trade-off(쓰기 비용, 스토리지, selectivity)를 설명해줘.",
  },
  {
    label: "락 충돌 진단",
    template: "-- the query that was reported as blocked",
    prompt:
      "**한국어로** 답변해줘. 이 쿼리가 락 대기 중이라고 보고됐어. 가장 가능성 높은 락 경합 원인을 진단하고 완화책을 제안해줘.",
  },
  {
    label: "성능 개선 리라이트",
    template: "-- original SQL",
    prompt:
      "**한국어로** 답변해줘. 이 SQL을 PostgreSQL에서 더 빠르게 돌도록 재작성하고, 각 변경이 왜 도움이 되는지 설명해줘. 정확한 시맨틱은 보존.",
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
  const { selected: clusterId, setSelected: setClusterId } =
    useSelectedCluster();
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

  // Saved-queries library (cross-device, DDB-backed). Distinct from the
  // localStorage-only "recent plans" list above: that tracks EXPLAIN
  // results; this tracks bookmarked SQL queries the DBA wants to re-run
  // later, possibly from a different machine.
  const [savedQueries, setSavedQueries] = useState<SavedQuerySummary[]>([]);
  const [saveModal, setSaveModal] = useState<{
    title: string;
    description: string;
    tags_csv: string;
    error: string | null;
    submitting: boolean;
  } | null>(null);

  useEffect(() => {
    setHistory(loadPlanHistory());
  }, []);

  // Saved-queries library — best-effort fetch; failures stay silent so
  // the page still works when the backend is mid-deploy.
  const refreshSavedQueries = useCallback(() => {
    listSavedQueries({ limit: 50 })
      .then(setSavedQueries)
      .catch((e) => console.warn("[query-lab] saved queries load failed", e));
  }, []);
  useEffect(() => {
    refreshSavedQueries();
  }, [refreshSavedQueries]);

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
        setExplainError({
          message: "클러스터를 먼저 선택하세요.",
          kind: "infra",
        });
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
      setInsight("AI 진단은 현재 PostgreSQL 플랜에 대해서만 제공됩니다.");
      return;
    }
    const summary = summarizePlanForLLM(arr[0]);
    const sqlBlock = lastSql
      ? `\n\nSQL:\n\`\`\`sql\n${lastSql.trim()}\n\`\`\``
      : "";
    const message =
      `너는 시니어 PostgreSQL 성능 전문가야. 아래는 EXPLAIN ANALYZE 결과를 구조화한 요약이야. ` +
      `**한국어로** 가장 큰 병목 한 가지를 찍고, 구체적인 개선안 2~3가지를 제안해줘 ` +
      `(인덱스 컬럼 목록, 쿼리 재작성, 스키마 변경, planner 설정 등 — 모호한 일반론 금지). ` +
      `답변은 250단어 이하로 간결하게.` +
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
        setAnalysis("먼저 클러스터를 선택하세요.");
        setTab("analysis");
        return;
      }
      setAnalysis("");
      setLoadingKind("bulk");
      setTab("analysis");
      const message =
        `너는 프로덕션 배포 전 SQL 일괄을 검토하는 시니어 DBA야. **한국어로** 답변해줘. ` +
        `아래 statements는 세미콜론으로 구분되어 있어 ` +
        `(문자열 안의 inline ;가 있을 수 있으니 단순 split이 아니라 SQL 파싱 판단을 사용).\n\n` +
        `각 statement마다 마크다운 테이블에 한 행씩 출력:\n` +
        `| # | Statement (앞 80자) | Risk | Notes |\n` +
        `Risk 값: **safe** (read-only / 파라미터 바인딩된 DML), **risky** (대량 스캔, WHERE 누락, ` +
        `락 헤비), **dangerous** (DDL, WHERE 없는 DROP/TRUNCATE/DELETE, hot 테이블에 ALTER TABLE).\n` +
        `Notes는 그 쿼리에 특화된 짧은 한 문장.\n` +
        `테이블 다음에 "Summary" 섹션 — dangerous 쿼리들의 인덱스 목록 + 추천하는 다음 단계 한 가지.\n\n` +
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
        setAnalysis("먼저 클러스터를 선택하세요.");
        setTab("analysis");
        return;
      }
      setAnalysis("");
      setLoadingKind("analyze");
      setTab("analysis");

      const intro =
        presetPrompt ||
        "**한국어로** 답변해줘. 이 SQL을 정확성, 성능, side effect 관점에서 분석하고, plan을 요청했다면 명확하게 요약해줘.";
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
        eyebrow="자동화"
        title="Query Lab"
        description="EXPLAIN 버튼은 plan tree를 바로 렌더링하고, AI 분석은 SQL을 agent에 보내 자연어 해석을 받아옵니다."
        actions={
          <div className="flex items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">
              cluster
            </label>
            <ClusterPicker selected={clusterId} />
          </div>
        }
      />

      <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-3 mb-4">
        <div className="text-[11px] text-zinc-500 uppercase tracking-wider mb-2">
          quick presets — 템플릿을 클립보드에 복사하고 AI 분석 프롬프트를
          준비합니다
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
              프리셋 해제
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

          {/* Saved-queries library — durable bookmark of the SQL the
              DBA wants to keep around across devices. Distinct from
              "recent plans" below, which tracks EXPLAIN runs. */}
          <div className="border border-zinc-800 bg-zinc-900/40">
            <div className="flex items-center justify-between px-3 py-2 border-b border-zinc-800">
              <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
                saved queries · {savedQueries.length}
              </div>
              <button
                onClick={() =>
                  setSaveModal({
                    title: "",
                    description: "",
                    tags_csv: "",
                    error: null,
                    submitting: false,
                  })
                }
                disabled={!lastSql.trim()}
                className="text-[10px] uppercase tracking-wider px-2 py-1 border border-zinc-700 text-zinc-300 hover:border-amber-500/60 hover:text-amber-200 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                title={
                  lastSql.trim()
                    ? "현재 편집기의 SQL을 라이브러리에 저장"
                    : "먼저 SQL을 작성하거나 EXPLAIN을 실행하세요"
                }
              >
                + 현재 SQL 저장
              </button>
            </div>
            {savedQueries.length === 0 ? (
              <div className="px-3 py-4 text-[11px] text-zinc-500">
                저장된 쿼리가 없습니다. 자주 쓰는 진단/감사 SQL을 라이브러리에
                넣어두면 다른 기기에서도 그대로 불러올 수 있습니다.
              </div>
            ) : (
              <div className="divide-y divide-zinc-800 max-h-72 overflow-y-auto">
                {savedQueries.map((q) => (
                  <div
                    key={q.id}
                    className="px-3 py-2 hover:bg-zinc-800/40 group"
                  >
                    <div className="flex items-baseline justify-between gap-2 mb-0.5">
                      <button
                        onClick={async () => {
                          try {
                            const detail = await fetchSavedQuery(q.id);
                            setPrefilledSql(detail.sql_text);
                            if (detail.cluster_id)
                              setClusterId(detail.cluster_id);
                          } catch (e) {
                            console.warn(
                              "[query-lab] load saved query failed",
                              e,
                            );
                          }
                        }}
                        className="text-xs text-zinc-100 truncate text-left hover:text-amber-200 transition-colors"
                        title={q.description || q.title}
                      >
                        {q.title}
                      </button>
                      <button
                        onClick={async () => {
                          if (!confirm(`"${q.title}" 삭제할까요?`)) return;
                          try {
                            await deleteSavedQuery(q.id);
                            refreshSavedQueries();
                          } catch (e) {
                            console.warn(
                              "[query-lab] delete saved query failed",
                              e,
                            );
                          }
                        }}
                        className="opacity-0 group-hover:opacity-100 text-[10px] text-zinc-500 hover:text-rose-400 transition flex-shrink-0"
                      >
                        삭제
                      </button>
                    </div>
                    {q.description && (
                      <div className="text-[11px] text-zinc-500 truncate">
                        {q.description}
                      </div>
                    )}
                    <div className="text-[10px] text-zinc-600 mt-0.5 flex items-center gap-1.5 flex-wrap">
                      {q.cluster_id && (
                        <span className="font-mono truncate max-w-[10rem]">
                          {q.cluster_id}
                        </span>
                      )}
                      {q.tags?.slice(0, 4).map((t) => (
                        <span
                          key={t}
                          className="px-1 py-px border border-zinc-800 text-zinc-500"
                        >
                          {t}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

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
                <div className="text-sm text-zinc-500">EXPLAIN 실행 중…</div>
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
                  에디터에 SELECT를 붙여넣고{" "}
                  <span className="text-amber-300">EXPLAIN</span> 버튼을
                  눌러주세요.
                </div>
              )}
              {hasPlan && (
                <>
                  <PlanTree plan={explain!.plan} />
                  <div className="mt-4 border border-zinc-800 bg-zinc-900/40">
                    <div className="flex items-center justify-between px-3 py-2 border-b border-zinc-800">
                      <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
                        AI 진단 — 이 plan 기준
                      </div>
                      <button
                        onClick={handleGetInsight}
                        disabled={insightLoading}
                        className="text-xs px-3 py-1 border border-sky-500/40 text-sky-300 hover:bg-sky-500/10 disabled:opacity-50 transition-colors"
                      >
                        {insightLoading
                          ? "분석 중…"
                          : insight
                            ? "다시 진단"
                            : "AI 진단 받기"}
                      </button>
                    </div>
                    <div className="p-3">
                      {!insight && !insightLoading && (
                        <div className="text-xs text-zinc-500">
                          <span className="text-sky-300">AI 진단 받기</span>{" "}
                          버튼을 누르면 이 plan에 맞춰 2~3단계 권장안을 받아볼
                          수 있어요 (raw plan이 아니라 구조화된 요약만 보내므로
                          토큰 비용 적음).
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
                  프리셋을 고르면 템플릿이 클립보드에 복사됩니다. 에디터에 SQL을
                  붙여넣고
                  <span className="text-sky-400"> AI 분석</span>을 누르세요.
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Save modal — small inline form rather than its own component
          because the state shape is one-off. SQL is taken implicitly
          from lastSql (set whenever EXPLAIN/Analyze ran). */}
      {saveModal && (
        <div
          className="fixed inset-0 z-50 bg-zinc-950/80 flex items-center justify-center p-6"
          onClick={() => setSaveModal(null)}
        >
          <div
            className="bg-zinc-900 border border-zinc-800 p-6 w-full max-w-lg"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-4">
              <div>
                <div className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">
                  쿼리 저장
                </div>
                <h3 className="text-base font-medium text-zinc-100">
                  현재 SQL을 라이브러리에 저장
                </h3>
              </div>
              <button
                onClick={() => setSaveModal(null)}
                className="text-zinc-500 hover:text-zinc-200 text-xl leading-none"
              >
                ×
              </button>
            </div>

            <div className="space-y-3">
              <div>
                <label className="text-[10px] uppercase tracking-wider text-zinc-500">
                  제목 <span className="text-rose-400">*</span>
                </label>
                <input
                  type="text"
                  value={saveModal.title}
                  onChange={(e) =>
                    setSaveModal({ ...saveModal, title: e.target.value })
                  }
                  placeholder="예: prod-pg-1 capacity probe"
                  maxLength={255}
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-100 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60"
                />
              </div>
              <div>
                <label className="text-[10px] uppercase tracking-wider text-zinc-500">
                  설명 (선택)
                </label>
                <input
                  type="text"
                  value={saveModal.description}
                  onChange={(e) =>
                    setSaveModal({ ...saveModal, description: e.target.value })
                  }
                  placeholder="목록에서 한 줄로 보일 메모"
                  maxLength={500}
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-300 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60"
                />
              </div>
              <div>
                <label className="text-[10px] uppercase tracking-wider text-zinc-500">
                  태그 (쉼표로 구분)
                </label>
                <input
                  type="text"
                  value={saveModal.tags_csv}
                  onChange={(e) =>
                    setSaveModal({ ...saveModal, tags_csv: e.target.value })
                  }
                  placeholder="capacity, audit, idx-recommend"
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-300 text-sm px-3 py-2 mt-1 font-mono focus:outline-none focus:border-amber-500/60"
                />
              </div>
              <div>
                <label className="text-[10px] uppercase tracking-wider text-zinc-500">
                  SQL 미리보기
                </label>
                <pre className="bg-zinc-950 border border-zinc-800 p-2 text-[11px] text-zinc-300 font-mono whitespace-pre-wrap break-all max-h-40 overflow-y-auto mt-1">
                  {lastSql || "(미리보기할 SQL이 없습니다)"}
                </pre>
              </div>
              {saveModal.error && (
                <div className="text-xs text-rose-400">{saveModal.error}</div>
              )}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  onClick={() => setSaveModal(null)}
                  className="text-xs px-4 py-2 border border-zinc-700 text-zinc-400 hover:text-zinc-100 transition-colors"
                >
                  취소
                </button>
                <button
                  onClick={async () => {
                    if (!saveModal.title.trim()) {
                      setSaveModal({
                        ...saveModal,
                        error: "제목을 입력하세요",
                      });
                      return;
                    }
                    if (!lastSql.trim()) {
                      setSaveModal({
                        ...saveModal,
                        error: "저장할 SQL이 없습니다",
                      });
                      return;
                    }
                    setSaveModal({
                      ...saveModal,
                      submitting: true,
                      error: null,
                    });
                    try {
                      await createSavedQuery({
                        cluster_id: clusterId || null,
                        title: saveModal.title.trim(),
                        description: saveModal.description.trim() || undefined,
                        sql_text: lastSql,
                        tags: saveModal.tags_csv
                          .split(",")
                          .map((t) => t.trim())
                          .filter(Boolean),
                      });
                      setSaveModal(null);
                      refreshSavedQueries();
                    } catch (e) {
                      setSaveModal({
                        ...saveModal,
                        submitting: false,
                        error: e instanceof Error ? e.message : String(e),
                      });
                    }
                  }}
                  disabled={saveModal.submitting}
                  className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors disabled:opacity-50"
                >
                  {saveModal.submitting ? "저장 중…" : "저장"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </PageBody>
  );
}
