"use client";

import { useEffect, useState } from "react";

interface QueryEditorProps {
  // Hits /api/explain directly and renders the structured plan tree.
  onExplain: (sql: string) => void;
  // Goes through chat for natural-language analysis.
  onAnalyze: (sql: string) => void;
  // Reviews multiple semicolon-separated SQL statements at once.
  onBulkReview: (sql: string) => void;
  // Asks the agent to propose a semantically-equivalent rewrite.
  onRewrite: (sql: string) => void;
  isLoading: boolean;
  // Which side button shows the spinner — null if idle.
  loadingKind?: "explain" | "analyze" | "bulk" | "rewrite" | null;
  // Used when restoring a plan from history or opening a shared link.
  initialSql?: string;
}

export function QueryEditor({
  onExplain,
  onAnalyze,
  onBulkReview,
  onRewrite,
  isLoading,
  loadingKind,
  initialSql,
}: QueryEditorProps) {
  const [sql, setSql] = useState(initialSql || "");

  // Restore SQL whenever the parent prefill changes (history click / shared link).
  useEffect(() => {
    if (initialSql !== undefined) setSql(initialSql);
  }, [initialSql]);

  return (
    <div className="bg-zinc-800 border border-zinc-700 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 border-b border-zinc-700 bg-zinc-850">
        <span className="text-xs text-zinc-400 font-mono">SQL Editor</span>
        <div className="flex gap-2">
          <button
            onClick={() => onExplain(sql)}
            disabled={!sql.trim() || isLoading}
            className="px-3 py-1 text-xs bg-amber-600 text-white rounded hover:bg-amber-500 disabled:opacity-50 transition-colors"
            title="EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) — renders as a tree"
          >
            {loadingKind === "explain" ? "running…" : "EXPLAIN"}
          </button>
          <button
            onClick={() => onAnalyze(sql)}
            disabled={!sql.trim() || isLoading}
            className="px-3 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-500 disabled:opacity-50 transition-colors"
            title="Send the SQL to the agent for natural-language analysis"
          >
            {loadingKind === "analyze" ? "실행 중..." : "AI 분석"}
          </button>
          <button
            onClick={() => onBulkReview(sql)}
            disabled={!sql.trim() || isLoading}
            className="px-3 py-1 text-xs bg-emerald-600 text-white rounded hover:bg-emerald-500 disabled:opacity-50 transition-colors"
            title="Paste multiple SQLs (semicolon-separated) — agent rates each as safe / risky / dangerous with notes"
          >
            {loadingKind === "bulk" ? "검수 중..." : "Bulk review"}
          </button>
          <button
            onClick={() => onRewrite(sql)}
            disabled={!sql.trim() || isLoading}
            className="px-3 py-1 text-xs bg-violet-600 text-white rounded hover:bg-violet-500 disabled:opacity-50 transition-colors"
            title="AI가 시맨틱을 보존하면서 성능 개선 재작성안을 제안합니다 (plan-only EXPLAIN 비교, 실행 없음)"
          >
            {loadingKind === "rewrite" ? "분석 중..." : "리라이팅 제안"}
          </button>
        </div>
      </div>
      <textarea
        value={sql}
        onChange={(e) => setSql(e.target.value)}
        placeholder="SELECT * FROM orders WHERE created_at > '2026-01-01'"
        className="w-full h-40 bg-zinc-900 text-zinc-100 font-mono text-sm p-4 resize-none focus:outline-none"
        spellCheck={false}
      />
    </div>
  );
}
