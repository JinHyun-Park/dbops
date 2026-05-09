"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { MessageList, type Message } from "./message-list";
import { streamChat } from "@/lib/agentcore-sse";
import { fetchClusters } from "@/lib/api-client";

interface ClusterRow {
  cluster_id: string;
  engine?: string;
}

export function ChatPanel() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [clusterId, setClusterId] = useState("");
  const [clusters, setClusters] = useState<ClusterRow[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    fetchClusters()
      .then((rows: ClusterRow[]) => {
        setClusters(rows);
        if (rows.length > 0) setClusterId(rows[0].cluster_id);
      })
      .catch((e) => console.error("Failed to load clusters:", e));
  }, []);

  const handleSend = useCallback(() => {
    if (!input.trim() || isStreaming) return;

    const userMsg: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content: input.trim(),
    };

    const assistantMsg: Message = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      toolCalls: [],
    };

    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setInput("");
    setIsStreaming(true);

    abortRef.current = streamChat(
      input.trim(),
      clusterId,
      (token) => {
        setMessages((prev) => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last.role === "assistant") {
            updated[updated.length - 1] = { ...last, content: last.content + token };
          }
          return updated;
        });
      },
      (name, status) => {
        setMessages((prev) => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last.role === "assistant") {
            const toolCalls = [...(last.toolCalls || [])];
            const existing = toolCalls.findIndex((tc) => tc.name === name);
            if (existing >= 0) {
              toolCalls[existing] = { name, status: status as "running" | "done" };
            } else {
              toolCalls.push({ name, status: status as "running" | "done" });
            }
            updated[updated.length - 1] = { ...last, toolCalls };
          }
          return updated;
        });
      },
      () => setIsStreaming(false),
      (err) => {
        console.error("Stream error:", err);
        setIsStreaming(false);
      },
    );
  }, [input, isStreaming, clusterId]);

  return (
    <div className="flex flex-col h-full bg-zinc-900">
      <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-800">
        <h1 className="text-lg font-semibold text-zinc-100">DBOps Chat</h1>
        <select
          value={clusterId}
          onChange={(e) => setClusterId(e.target.value)}
          className="bg-zinc-800 text-zinc-300 border border-zinc-700 rounded px-3 py-1.5 text-sm"
        >
          {clusters.length === 0 && <option value="">No clusters</option>}
          {clusters.map((c) => (
            <option key={c.cluster_id} value={c.cluster_id}>
              {c.cluster_id} {c.engine ? `(${c.engine})` : ""}
            </option>
          ))}
        </select>
      </div>

      <div className="flex-1 overflow-y-auto">
        {messages.length === 0 ? (
          <div className="flex items-center justify-center h-full text-zinc-500">
            Aurora 클러스터에 대해 질문하세요
          </div>
        ) : (
          <MessageList messages={messages} />
        )}
      </div>

      <div className="border-t border-zinc-800 p-4">
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
            placeholder="예: prod-cluster의 slow query를 분석해줘"
            className="flex-1 bg-zinc-800 text-zinc-100 border border-zinc-700 rounded-lg px-4 py-3 focus:outline-none focus:ring-2 focus:ring-blue-500"
            disabled={isStreaming}
          />
          <button
            onClick={handleSend}
            disabled={isStreaming || !input.trim()}
            className="px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {isStreaming ? "..." : "전송"}
          </button>
        </div>
      </div>
    </div>
  );
}
