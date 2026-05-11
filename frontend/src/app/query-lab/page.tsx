"use client";

import { useState, useCallback, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { QueryEditor } from "@/components/query-lab/query-editor";
import { streamChat } from "@/lib/agentcore-sse";
import { fetchClusters } from "@/lib/api-client";
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

export default function QueryLabPage() {
  const [clusters, setClusters] = useState<ClusterRow[]>([]);
  const [clusterId, setClusterId] = useState<string>("");
  const [result, setResult] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [presetPrompt, setPresetPrompt] = useState<string>("");

  useEffect(() => {
    fetchClusters()
      .then((rows: ClusterRow[]) => {
        setClusters(rows);
        if (rows.length > 0) setClusterId(rows[0].cluster_id);
      })
      .catch(console.error);
  }, []);

  const handleSubmit = useCallback(
    (sql: string) => {
      if (!clusterId) {
        setResult("Select a cluster first.");
        return;
      }
      setResult("");
      setIsLoading(true);

      const intro =
        presetPrompt ||
        "Analyze this SQL for correctness, performance, and side effects. If a plan is requested, summarize it clearly.";
      const message = `${intro}\n\n\`\`\`sql\n${sql}\n\`\`\``;

      streamChat(
        message,
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
    [clusterId, presetPrompt],
  );

  const applyPreset = (template: string, prompt: string) => {
    setPresetPrompt(prompt);
    setResult("");
    navigator.clipboard?.writeText(template).catch(() => {});
  };

  return (
    <PageBody>
      <PageHeader
        eyebrow="automate"
        title="Query Lab"
        description="에이전트가 EXPLAIN/index/lock/rewrite 분석을 수행. preset 버튼이 prompt와 template을 동시에 세팅합니다."
        actions={
          <div className="flex items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">cluster</label>
            <select
              value={clusterId}
              onChange={(e) => setClusterId(e.target.value)}
              className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-1.5 focus:outline-none focus:border-amber-500/60"
            >
              {clusters.length === 0 && <option value="">(no clusters registered)</option>}
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
          quick presets — copies a template to clipboard and primes the analysis prompt
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
          <div className="mt-2 text-[11px] text-sky-400 font-mono">prompt: {presetPrompt}</div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div>
          <QueryEditor onSubmit={handleSubmit} isLoading={isLoading} />
        </div>
        <div className="bg-zinc-900/50 border border-zinc-800 p-4 overflow-y-auto max-h-[80vh]">
          <div className="flex items-center justify-between mb-3">
            <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
              {presetPrompt ? PRESETS.find((p) => p.prompt === presetPrompt)?.label || "preset analysis" : "ai analysis"}
              {clusterId && <span className="ml-2 text-zinc-700">·</span>}
              {clusterId && <span className="ml-2 text-zinc-500 normal-case tracking-normal font-sans">{clusterId}</span>}
            </div>
            {presetPrompt && (
              <span className="text-[10px] px-2 py-0.5 border border-amber-500/40 text-amber-300">
                preset active
              </span>
            )}
          </div>
          {result ? (
            <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-zinc-950 prose-pre:border prose-pre:border-zinc-800 prose-code:text-sky-300">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{result}</ReactMarkdown>
            </div>
          ) : (
            <div className="text-sm text-zinc-500">
              Pick a preset (template copied to clipboard), paste SQL in the editor, then click
              analyze.
            </div>
          )}
        </div>
      </div>
    </PageBody>
  );
}
