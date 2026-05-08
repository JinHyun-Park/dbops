"use client";

import { useState } from "react";

interface QueryEditorProps {
  onSubmit: (sql: string) => void;
  isLoading: boolean;
}

export function QueryEditor({ onSubmit, isLoading }: QueryEditorProps) {
  const [sql, setSql] = useState("");

  return (
    <div className="bg-zinc-800 border border-zinc-700 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 border-b border-zinc-700 bg-zinc-850">
        <span className="text-xs text-zinc-400 font-mono">SQL Editor</span>
        <div className="flex gap-2">
          <button
            onClick={() => onSubmit(`EXPLAIN ANALYZE ${sql}`)}
            disabled={!sql.trim() || isLoading}
            className="px-3 py-1 text-xs bg-amber-600 text-white rounded hover:bg-amber-500 disabled:opacity-50 transition-colors"
          >
            EXPLAIN
          </button>
          <button
            onClick={() => onSubmit(sql)}
            disabled={!sql.trim() || isLoading}
            className="px-3 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-500 disabled:opacity-50 transition-colors"
          >
            {isLoading ? "실행 중..." : "AI 분석"}
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
