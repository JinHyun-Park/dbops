"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { MessageList, type Message } from "./message-list";
import { streamChat } from "@/lib/agentcore-sse";
import { fetchClusters, fetchModels } from "@/lib/api-client";

interface ClusterRow {
  cluster_id: string;
  engine?: string;
}

interface Conversation {
  id: string;
  title: string;
  cluster_id: string;
  updated_at: number;
  messages: Message[];
}

interface ModelOption {
  id: string;
  label: string;
}

// Hardcoded fallback only used if /api/models can't be reached (cold start, IAM, etc.).
// Live models are pulled from Bedrock ListInferenceProfiles via /api/models so the
// dropdown always reflects what Bedrock will actually accept.
const FALLBACK_MODELS: ModelOption[] = [
  { id: "global.anthropic.claude-opus-4-7", label: "Opus 4.7" },
  { id: "global.anthropic.claude-opus-4-6-v1", label: "Opus 4.6" },
  { id: "global.anthropic.claude-opus-4-5-20251101-v1:0", label: "Opus 4.5" },
  { id: "global.anthropic.claude-sonnet-4-6", label: "Sonnet 4.6" },
  { id: "global.anthropic.claude-sonnet-4-5-20250929-v1:0", label: "Sonnet 4.5" },
  { id: "global.anthropic.claude-haiku-4-5-20251001-v1:0", label: "Haiku 4.5" },
];

// Default to Sonnet 4.6 — fastest verified valid generation.
const DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-6";
const MODEL_STORAGE_KEY = "dbops_chat_model";
const STORAGE_KEY = "dbops_conversations_v1";

function loadConversations(): Conversation[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Conversation[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveConversations(conversations: Conversation[]): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
  } catch {
    // quota exceeded; trim oldest
    try {
      const trimmed = [...conversations].sort((a, b) => b.updated_at - a.updated_at).slice(0, 30);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
    } catch {
      // ignore
    }
  }
}

function newConversation(clusterId: string): Conversation {
  return {
    id: `dbops-session-${crypto.randomUUID()}`,
    title: "New conversation",
    cluster_id: clusterId,
    updated_at: Date.now(),
    messages: [],
  };
}

function conversationToMarkdown(conv: Conversation): string {
  const header = [
    `# ${conv.title}`,
    ``,
    `- **Cluster**: ${conv.cluster_id || "n/a"}`,
    `- **Exported**: ${new Date().toISOString()}`,
    `- **Messages**: ${conv.messages.length}`,
    ``,
    `---`,
    ``,
  ].join("\n");
  const body = conv.messages
    .map((m) => {
      const role = m.role === "user" ? "**User**" : "**Assistant**";
      const tools =
        m.toolCalls && m.toolCalls.length > 0
          ? `\n_Tools_: ${m.toolCalls.map((t) => `\`${t.name}\` (${t.status})`).join(", ")}\n`
          : "";
      return `### ${role}\n${tools}\n${m.content || "(empty)"}\n`;
    })
    .join("\n---\n\n");
  return header + body;
}

function downloadBlob(filename: string, mime: string, content: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function slugify(s: string): string {
  return (s || "conversation")
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .slice(0, 60)
    || "conversation";
}

function relTime(ms: number): string {
  const diff = Date.now() - ms;
  if (diff < 60_000) return "now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h`;
  return `${Math.floor(diff / 86_400_000)}d`;
}

export function ChatPanel() {
  const [clusters, setClusters] = useState<ClusterRow[]>([]);
  const [clusterId, setClusterId] = useState("");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [modelId, setModelId] = useState<string>(DEFAULT_MODEL);
  const [availableModels, setAvailableModels] = useState<ModelOption[]>(FALLBACK_MODELS);
  const [followupsLoading, setFollowupsLoading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const followupAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    fetchModels()
      .then((d) => {
        const live = (d.models || []).map((m) => ({ id: m.id, label: m.label }));
        if (live.length > 0) setAvailableModels(live);
        // Validate stored model against live list.
        const stored = localStorage.getItem(MODEL_STORAGE_KEY);
        const candidates = live.length > 0 ? live : FALLBACK_MODELS;
        if (stored && candidates.find((m) => m.id === stored)) {
          setModelId(stored);
        } else {
          setModelId(d.default || candidates[0].id);
        }
      })
      .catch((e) => {
        console.warn("models fetch failed; using fallback", e);
        const stored = localStorage.getItem(MODEL_STORAGE_KEY);
        if (stored && FALLBACK_MODELS.find((m) => m.id === stored)) setModelId(stored);
      });
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") localStorage.setItem(MODEL_STORAGE_KEY, modelId);
  }, [modelId]);

  useEffect(() => {
    const stored = loadConversations();
    setConversations(stored);
    if (stored.length > 0) {
      setActiveId(stored[0].id);
      if (stored[0].cluster_id) setClusterId(stored[0].cluster_id);
    }
  }, []);

  useEffect(() => {
    fetchClusters()
      .then((rows: ClusterRow[]) => {
        setClusters(rows);
        if (rows.length > 0 && !clusterId) setClusterId(rows[0].cluster_id);
      })
      .catch((e) => console.error("Failed to load clusters:", e));
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const sp = new URLSearchParams(window.location.search);
    const prompt = sp.get("prompt");
    if (prompt) {
      setInput(prompt);
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  const active = conversations.find((c) => c.id === activeId);
  const messages = active?.messages || [];

  const persist = useCallback((updater: (prev: Conversation[]) => Conversation[]) => {
    setConversations((prev) => {
      const next = updater(prev);
      saveConversations(next);
      return next;
    });
  }, []);

  const startNewConversation = useCallback(() => {
    const conv = newConversation(clusterId);
    persist((prev) => [conv, ...prev]);
    setActiveId(conv.id);
    setInput("");
  }, [clusterId, persist]);

  const removeConversation = useCallback(
    (id: string) => {
      persist((prev) => prev.filter((c) => c.id !== id));
      if (activeId === id) {
        setActiveId((prevId) => {
          const remaining = conversations.filter((c) => c.id !== id);
          return remaining.length > 0 ? remaining[0].id : "";
        });
      }
    },
    [activeId, conversations, persist],
  );

  // Generate 2-3 follow-up questions on a throwaway session so the main
  // conversation memory isn't polluted. Best-effort — failures are silent.
  const generateFollowups = useCallback(
    (convId: string, userText: string, assistantText: string) => {
      // Skip if the answer is short (likely an error or one-liner).
      if (assistantText.trim().length < 80) return;
      followupAbortRef.current?.abort();
      setFollowupsLoading(true);
      const prompt =
        `Suggest 3 short, specific follow-up questions a DBA might ask next, based on the Q&A below. Return ONLY a JSON array of 3 strings — no other text, no markdown, no code fences. Example: ["q1","q2","q3"].\n\n` +
        `Q: ${userText}\n\nA: ${assistantText.slice(0, 4000)}`;
      let buffer = "";
      followupAbortRef.current = streamChat(
        prompt,
        "",
        (token) => {
          buffer += token;
        },
        () => {},
        () => {
          setFollowupsLoading(false);
          // Extract first JSON array in the buffer.
          const match = buffer.match(/\[[\s\S]*?\]/);
          if (!match) return;
          let parsed: unknown;
          try {
            parsed = JSON.parse(match[0]);
          } catch {
            return;
          }
          if (!Array.isArray(parsed)) return;
          const followups = parsed
            .filter((q): q is string => typeof q === "string" && q.trim().length > 0)
            .slice(0, 3)
            .map((q) => q.trim());
          if (followups.length === 0) return;
          persist((prev) =>
            prev.map((c) => {
              if (c.id !== convId) return c;
              const msgs = [...c.messages];
              const last = msgs[msgs.length - 1];
              if (last && last.role === "assistant") {
                msgs[msgs.length - 1] = { ...last, followups };
              }
              return { ...c, messages: msgs };
            }),
          );
        },
        () => {
          setFollowupsLoading(false);
        },
        // Throwaway session id so the agent's memory stays clean.
        `followup-${convId}-${Date.now()}`,
        modelId,
      );
    },
    [modelId, persist],
  );

  const sendText = useCallback(
    (raw: string) => {
      const userText = raw.trim();
      if (!userText || isStreaming) return;

      // Cancel any in-flight followup generation for the previous turn.
      followupAbortRef.current?.abort();

      // Ensure there is an active conversation.
      let convId = activeId;
      if (!convId) {
        const conv = newConversation(clusterId);
        convId = conv.id;
        persist((prev) => [conv, ...prev]);
        setActiveId(conv.id);
      }

      const userMsg: Message = { id: crypto.randomUUID(), role: "user", content: userText };
      const assistantMsg: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: "",
        toolCalls: [],
      };

      persist((prev) =>
        prev.map((c) => {
          if (c.id !== convId) return c;
          // Clear followups on the previous assistant message so chips don't
          // linger above the new turn.
          const cleared = c.messages.map((m, i) =>
            i === c.messages.length - 1 && m.role === "assistant" && m.followups
              ? { ...m, followups: undefined }
              : m,
          );
          return {
            ...c,
            cluster_id: clusterId || c.cluster_id,
            title: cleared.length === 0 ? userText.slice(0, 50) : c.title,
            updated_at: Date.now(),
            messages: [...cleared, userMsg, assistantMsg],
          };
        }),
      );
      setIsStreaming(true);

      abortRef.current = streamChat(
        userText,
        clusterId,
        (token) => {
          persist((prev) =>
            prev.map((c) => {
              if (c.id !== convId) return c;
              const msgs = [...c.messages];
              const last = msgs[msgs.length - 1];
              if (last && last.role === "assistant") {
                msgs[msgs.length - 1] = { ...last, content: last.content + token };
              }
              return { ...c, messages: msgs, updated_at: Date.now() };
            }),
          );
        },
        (name, status) => {
          persist((prev) =>
            prev.map((c) => {
              if (c.id !== convId) return c;
              const msgs = [...c.messages];
              const last = msgs[msgs.length - 1];
              if (last && last.role === "assistant") {
                const toolCalls = [...(last.toolCalls || [])];
                const existing = toolCalls.findIndex((tc) => tc.name === name);
                if (existing >= 0) {
                  toolCalls[existing] = { name, status: status as "running" | "done" };
                } else {
                  toolCalls.push({ name, status: status as "running" | "done" });
                }
                msgs[msgs.length - 1] = { ...last, toolCalls };
              }
              return { ...c, messages: msgs };
            }),
          );
        },
        () => {
          setIsStreaming(false);
          // Pull the final assistant text from state at the moment we finished.
          setConversations((prev) => {
            const conv = prev.find((c) => c.id === convId);
            const finalAssistant = conv?.messages[conv.messages.length - 1];
            if (finalAssistant && finalAssistant.role === "assistant") {
              generateFollowups(convId, userText, finalAssistant.content);
            }
            return prev;
          });
        },
        (err) => {
          console.error("Stream error:", err);
          setStreamError(err?.message || "Unknown stream error");
          setIsStreaming(false);
        },
        convId,
        modelId,
      );
    },
    [isStreaming, clusterId, activeId, modelId, persist, generateFollowups],
  );

  const handleSend = useCallback(() => {
    if (!input.trim()) return;
    const text = input;
    setInput("");
    sendText(text);
  }, [input, sendText]);

  // clear stale error when the user starts typing
  useEffect(() => {
    if (input && streamError) setStreamError(null);
  }, [input, streamError]);

  return (
    <div className="flex h-[calc(100vh-3.25rem)]">
      <aside className="hidden lg:flex w-64 flex-col border-r border-zinc-800 bg-zinc-950/60 chat-sidebar">
        <div className="px-4 py-3 border-b border-zinc-800 flex items-center justify-between">
          <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
            conversations
          </div>
          <div className="flex items-center gap-1.5">
            {conversations.length > 0 && (
              <button
                onClick={() => {
                  if (window.confirm(`Delete all ${conversations.length} conversations? This cannot be undone.`)) {
                    persist(() => []);
                    setActiveId("");
                  }
                }}
                className="text-[10px] px-2 py-1 border border-zinc-700 text-zinc-500 hover:border-rose-500/40 hover:text-rose-400 transition-colors"
                title="clear all"
              >
                clear all
              </button>
            )}
            <button
              onClick={startNewConversation}
              className="text-xs px-2 py-1 border border-amber-500/40 text-amber-300 hover:bg-amber-500/10 transition-colors"
            >
              + new
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-2 py-2 space-y-0.5">
          {conversations.length === 0 ? (
            <div className="px-3 py-6 text-center text-[11px] text-zinc-600">
              click <span className="text-amber-400">+ new</span> to start
            </div>
          ) : (
            conversations
              .sort((a, b) => b.updated_at - a.updated_at)
              .map((c) => (
                <button
                  key={c.id}
                  onClick={() => setActiveId(c.id)}
                  className={`group w-full text-left px-3 py-2 transition-colors ${
                    c.id === activeId
                      ? "bg-zinc-800/80 text-zinc-100"
                      : "text-zinc-400 hover:bg-zinc-800/40 hover:text-zinc-200"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="text-xs truncate">{c.title}</div>
                      <div className="text-[10px] text-zinc-600 mt-0.5 truncate font-mono">
                        {c.cluster_id || "—"}
                      </div>
                    </div>
                    <div className="flex flex-col items-end gap-1">
                      <span className="text-[10px] text-zinc-600">{relTime(c.updated_at)}</span>
                      <span
                        onClick={(e) => {
                          e.stopPropagation();
                          removeConversation(c.id);
                        }}
                        className="text-zinc-700 hover:text-rose-400 opacity-0 group-hover:opacity-100 transition-opacity text-xs cursor-pointer"
                        title="delete"
                      >
                        ×
                      </span>
                    </div>
                  </div>
                </button>
              ))
          )}
        </div>
      </aside>

      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex items-center justify-between px-6 py-3 border-b border-zinc-800 gap-4 flex-wrap chat-header">
          <div className="min-w-0">
            <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
              chat
              <span className="ml-2 text-zinc-700">·</span>
              <span className="ml-2 normal-case tracking-normal">
                Claude {availableModels.find((m) => m.id === modelId)?.label || "(custom)"}
              </span>
            </div>
            <div className="text-sm text-zinc-200 mt-0.5 truncate">
              {active ? active.title : "Start a new conversation"}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">model</label>
            <select
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
              className="bg-zinc-900 text-zinc-200 border border-zinc-800 px-3 py-1.5 text-sm focus:outline-none focus:border-amber-500/60"
              title={modelId}
            >
              {availableModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">cluster</label>
            <select
              value={clusterId}
              onChange={(e) => setClusterId(e.target.value)}
              className="bg-zinc-900 text-zinc-200 border border-zinc-800 px-3 py-1.5 text-sm focus:outline-none focus:border-amber-500/60"
            >
              {clusters.length === 0 && <option value="">no clusters</option>}
              {clusters.map((c) => (
                <option key={c.cluster_id} value={c.cluster_id}>
                  {c.cluster_id}
                </option>
              ))}
            </select>
            {active && active.messages.length > 0 && (
              <>
                <button
                  onClick={() => {
                    if (!active) return;
                    const md = conversationToMarkdown(active);
                    const stamp = new Date().toISOString().slice(0, 10);
                    downloadBlob(
                      `dbops-${slugify(active.title)}-${stamp}.md`,
                      "text/markdown;charset=utf-8",
                      md,
                    );
                  }}
                  className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-amber-500/40 hover:text-amber-300 transition-colors"
                  title="Download conversation as Markdown (.md)"
                >
                  ⬇ md
                </button>
                <button
                  onClick={() => {
                    if (typeof window !== "undefined") window.print();
                  }}
                  className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-amber-500/40 hover:text-amber-300 transition-colors"
                  title="Print or save as PDF via the browser print dialog"
                >
                  🖨 pdf
                </button>
                <button
                  onClick={() => {
                    if (!active) return;
                    if (window.confirm("Clear all messages in this conversation?")) {
                      persist((prev) =>
                        prev.map((c) =>
                          c.id === active.id
                            ? { ...c, messages: [], title: "New conversation", updated_at: Date.now() }
                            : c,
                        ),
                      );
                    }
                  }}
                  className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-rose-500/40 hover:text-rose-400 transition-colors"
                >
                  clear messages
                </button>
              </>
            )}
            {active && (
              <button
                onClick={() => {
                  if (!active) return;
                  if (window.confirm("Delete this conversation?")) {
                    removeConversation(active.id);
                  }
                }}
                className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-rose-500/40 hover:text-rose-400 transition-colors"
              >
                delete
              </button>
            )}
            <button
              onClick={startNewConversation}
              className="lg:hidden text-xs px-3 py-1.5 border border-amber-500/40 text-amber-300 hover:bg-amber-500/10 transition-colors"
            >
              + new
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto chat-printable">
          {messages.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-center px-6">
              <div className="font-mono text-[10px] tracking-[0.2em] uppercase text-zinc-600 mb-3">
                conversation primer
              </div>
              <div className="text-zinc-300 text-lg max-w-md mb-6">
                자연어로 Aurora 운영을 위임하세요. agent가 MCP 툴로 메트릭/스키마/EXPLAIN을 호출합니다.
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2 max-w-2xl w-full">
                {[
                  "최근 1시간 동안 가장 느린 쿼리 5개 분석해줘",
                  "현재 클러스터의 health score는?",
                  "blocking lock 있으면 보여줘",
                  "vacuum 안 된 테이블 찾아줘",
                ].map((p) => (
                  <button
                    key={p}
                    onClick={() => setInput(p)}
                    className="text-left text-xs text-zinc-400 hover:text-zinc-100 border border-zinc-800 hover:border-amber-500/40 px-3 py-2 transition-colors"
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <MessageList
              messages={messages}
              onFollowupClick={(text) => sendText(text)}
              followupsLoading={followupsLoading}
            />
          )}
        </div>

        <div className="border-t border-zinc-800 p-4 chat-input">
          {streamError && (
            <div className="mb-2 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
              stream error: <span className="font-mono">{streamError}</span>
            </div>
          )}
          <div className="flex gap-2">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
              placeholder="예: prod-cluster의 slow query를 분석해줘"
              className="flex-1 bg-zinc-900 text-zinc-100 border border-zinc-800 rounded px-4 py-3 focus:outline-none focus:border-amber-500/60 transition-colors"
              disabled={isStreaming}
            />
            <button
              onClick={handleSend}
              disabled={isStreaming || !input.trim()}
              className="px-6 py-3 bg-amber-500 text-zinc-900 font-medium rounded hover:bg-amber-400 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {isStreaming ? "…" : "send"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
