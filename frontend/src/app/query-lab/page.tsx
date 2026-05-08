"use client";

import { useState, useCallback } from "react";
import { QueryEditor } from "@/components/query-lab/query-editor";
import { streamChat } from "@/lib/agentcore-sse";

export default function QueryLabPage() {
  const [clusterId] = useState("default-cluster");
  const [result, setResult] = useState("");
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = useCallback(
    (sql: string) => {
      setResult("");
      setIsLoading(true);

      streamChat(
        `다음 SQL을 분석해줘:\n\`\`\`sql\n${sql}\n\`\`\``,
        clusterId,
        (token) => setResult((prev) => prev + token),
        () => {},
        () => setIsLoading(false),
        (err) => {
          setResult(`Error: ${err.message}`);
          setIsLoading(false);
        },
      );
    },
    [clusterId],
  );

  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 p-6">
      <h1 className="text-2xl font-bold mb-6">Query Lab</h1>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div>
          <QueryEditor onSubmit={handleSubmit} isLoading={isLoading} />
        </div>
        <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-4">
          <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">AI 분석 결과</div>
          {result ? (
            <div className="text-sm text-zinc-200 whitespace-pre-wrap font-mono">{result}</div>
          ) : (
            <div className="text-sm text-zinc-500">SQL을 입력하고 분석 버튼을 눌러주세요</div>
          )}
        </div>
      </div>
    </div>
  );
}
