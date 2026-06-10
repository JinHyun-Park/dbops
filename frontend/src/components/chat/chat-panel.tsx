"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { MessageList, type Message } from "./message-list";
import { streamChat } from "@/lib/agentcore-sse";
import { takeRcaHandoff } from "@/lib/rca-handoff";
import { SearchableClusterSelect } from "@/components/design-system/searchable-cluster-select";
import {
  fetchClusters,
  fetchModels,
  createRunbook,
  listChatSessions,
  fetchChatSession,
  putChatSession,
  deleteChatSession,
} from "@/lib/api-client";

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
  {
    id: "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    label: "Sonnet 4.5",
  },
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
      const trimmed = [...conversations]
        .sort((a, b) => b.updated_at - a.updated_at)
        .slice(0, 30);
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
          ? `\n_Tools_: ${m.toolCalls
              .map((t) => `\`${t.name}\` (${t.status})`)
              .join(", ")}\n`
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
  return (
    (s || "conversation")
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, "")
      .replace(/\s+/g, "-")
      .slice(0, 60) || "conversation"
  );
}

function escapeHtml(s: string): string {
  return s.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ]!,
  );
}

function renderMarkdownToHtml(content: string): string {
  // Reuse the same markdown stack as the chat bubble (react-markdown +
  // remark-gfm) so the PDF rendering matches what the user sees on screen.
  if (!content) return "<p><em>(empty)</em></p>";
  return renderToStaticMarkup(
    <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>,
  );
}

function buildConversationHtml(conv: Conversation): string {
  const msgs = conv.messages
    .map((m) => {
      const role = m.role === "user" ? "User" : "Assistant";
      const cls = m.role === "user" ? "msg msg-user" : "msg msg-assistant";
      const tools =
        m.toolCalls && m.toolCalls.length > 0
          ? `<div class="tools">Tools: ${m.toolCalls
              .map(
                (t) =>
                  `<code>${escapeHtml(
                    t.name,
                  )}</code> <span class="tool-status">(${escapeHtml(
                    t.status,
                  )})</span>`,
              )
              .join(", ")}</div>`
          : "";
      const body =
        m.role === "user"
          ? `<div class="content user-text">${escapeHtml(
              m.content || "(empty)",
            )}</div>`
          : `<div class="content">${renderMarkdownToHtml(m.content)}</div>`;
      return `<section class="${cls}"><div class="role">${role}</div>${tools}${body}</section>`;
    })
    .join("\n");

  return `<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>${escapeHtml(conv.title)} — DBOps Chat</title>
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
      max-width: 760px;
      margin: 0 auto;
      padding: 28px;
      color: #16161a;
      line-height: 1.55;
      background: #fff;
    }
    h1 { font-size: 1.55em; margin: 0 0 0.3em; }
    .meta { color: #6b6a62; font-size: 0.82em; margin-bottom: 1.8em; padding-bottom: 1em; border-bottom: 1px solid #ddd; }
    .meta strong { color: #16161a; }
    .msg { margin: 1.3em 0; padding: 0.85em 1.05em; border-radius: 6px; page-break-inside: avoid; }
    .msg-user { background: #eef3fb; border-left: 3px solid #1d4ed8; }
    .msg-assistant { background: #faf9f4; border-left: 3px solid #d97706; }
    .role { font-weight: 700; font-size: 0.7em; text-transform: uppercase; letter-spacing: 0.1em; color: #555; margin-bottom: 0.4em; }
    .tools { font-size: 0.78em; color: #555; margin-bottom: 0.55em; }
    .tools code { background: #ebe9e2; padding: 0.08em 0.35em; border-radius: 3px; font-size: 0.9em; }
    .tool-status { color: #777; }
    .content > :first-child { margin-top: 0; }
    .content > :last-child { margin-bottom: 0; }
    .user-text { white-space: pre-wrap; }
    pre {
      background: #1a1a18; color: #f4f3ef;
      padding: 0.85em 1em; border-radius: 5px;
      overflow-x: auto; font-size: 0.83em; line-height: 1.45;
      white-space: pre-wrap; word-break: break-word;
    }
    code { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
    :not(pre) > code { background: #ebe9e2; color: #b45309; padding: 0.08em 0.35em; border-radius: 3px; font-size: 0.9em; }
    table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.9em; }
    th, td { border: 1px solid #ddd; padding: 0.45em 0.7em; text-align: left; }
    th { background: #f3f3f0; font-weight: 600; }
    blockquote { margin: 1em 0; padding: 0.4em 1em; border-left: 3px solid #d1d1c8; color: #444; background: #f7f6f1; }
    a { color: #1d4ed8; }
    h1, h2, h3, h4 { line-height: 1.3; margin-top: 0.9em; }
    h2 { font-size: 1.2em; } h3 { font-size: 1.05em; } h4 { font-size: 0.95em; }
    @media print {
      body { padding: 0; max-width: none; }
      .msg { break-inside: avoid; }
      pre { break-inside: avoid; }
    }
  </style>
</head>
<body>
  <h1>${escapeHtml(conv.title)}</h1>
  <div class="meta">
    Cluster: <strong>${escapeHtml(conv.cluster_id || "n/a")}</strong>
    &nbsp;·&nbsp; Exported: ${new Date().toLocaleString()}
    &nbsp;·&nbsp; ${conv.messages.length} messages
  </div>
  ${msgs}
</body>
</html>`;
}

/**
 * Render the conversation into an off-screen iframe and trigger its print
 * dialog. The iframe contains ONLY the chat content (no app chrome, no
 * scroll container), so the OS print preview shows every message across
 * however many pages it needs — none of the "captures the whole webpage,
 * one page only" issues that `window.print()` on the live page had.
 */
function exportConversationToPdf(conv: Conversation) {
  if (typeof window === "undefined") return;
  const html = buildConversationHtml(conv);

  const iframe = document.createElement("iframe");
  Object.assign(iframe.style, {
    position: "fixed",
    right: "0",
    bottom: "0",
    width: "0",
    height: "0",
    border: "0",
    opacity: "0",
    pointerEvents: "none",
  } as Partial<CSSStyleDeclaration>);
  iframe.setAttribute("aria-hidden", "true");
  document.body.appendChild(iframe);

  const doc = iframe.contentDocument || iframe.contentWindow?.document;
  if (!doc) {
    iframe.remove();
    // Fall back to opening the HTML in a new tab if the iframe pathway fails.
    const blob = new Blob([html], { type: "text/html;charset=utf-8" });
    window.open(URL.createObjectURL(blob), "_blank", "noopener,noreferrer");
    return;
  }

  doc.open();
  doc.write(html);
  doc.close();

  const trigger = () => {
    try {
      iframe.contentWindow?.focus();
      iframe.contentWindow?.print();
    } finally {
      // Give the print dialog a moment to grab the document before we remove
      // the iframe — Safari otherwise cancels the print on rapid removal.
      window.setTimeout(() => iframe.remove(), 2000);
    }
  };

  // If the document loaded synchronously, fire on the next tick; otherwise
  // wait for the iframe's load event.
  if (doc.readyState === "complete") {
    window.setTimeout(trigger, 50);
  } else {
    iframe.addEventListener("load", () => window.setTimeout(trigger, 50), {
      once: true,
    });
  }
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
  const [availableModels, setAvailableModels] =
    useState<ModelOption[]>(FALLBACK_MODELS);
  const [followupsLoading, setFollowupsLoading] = useState(false);

  // Save-as-Runbook modal. Stays here (vs MessageList) so a save in
  // progress survives a re-render of the streaming message list.
  const [runbookDraft, setRunbookDraft] = useState<{
    title: string;
    body_md: string;
    tags_csv: string;
    cluster_id: string;
  } | null>(null);
  const [runbookSaving, setRunbookSaving] = useState(false);
  const [runbookSaveError, setRunbookSaveError] = useState<string | null>(null);
  const [runbookSavedToast, setRunbookSavedToast] = useState<number | null>(
    null,
  );
  const abortRef = useRef<AbortController | null>(null);
  const followupAbortRef = useRef<AbortController | null>(null);
  // When the chat is deep-linked from another page (e.g. the dashboard/timeline
  // "AI 근본원인 분석" button → /chat?cluster=…&prompt=…), pin that cluster so
  // the async cluster-defaults below don't clobber it.
  const deepLinkClusterRef = useRef<string | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    fetchModels()
      .then((d) => {
        const live = (d.models || []).map((m) => ({
          id: m.id,
          label: m.label,
        }));
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
        if (stored && FALLBACK_MODELS.find((m) => m.id === stored))
          setModelId(stored);
      });
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined")
      localStorage.setItem(MODEL_STORAGE_KEY, modelId);
  }, [modelId]);

  // Initial conversation load: localStorage is the warm cache, DDB is the
  // source of truth. Render the local copy first so the UI doesn't flash
  // empty while the API resolves, then replace with the merged view once
  // the server responds. Sessions in both: server wins. Server-only: pulled
  // down on demand when the user clicks them (avoids fanning out N detail
  // fetches). Local-only: pushed up so they're durable across devices.
  useEffect(() => {
    let stored = loadConversations();
    // RCA handoff: arriving from the RCA side panel's "전체 대화로 이어가기".
    // Materialize the question + the already-streamed analysis as a NEW
    // conversation HERE — before the DDB merge below — so it's part of `stored`
    // and survives the merge (a separate effect would race and get clobbered),
    // and as the newest entry it auto-activates.
    const handoff = takeRcaHandoff();
    if (handoff) {
      const conv: Conversation = {
        id: `dbops-session-${crypto.randomUUID()}`,
        title: `RCA · ${handoff.cluster_id}`.slice(0, 50),
        cluster_id: handoff.cluster_id,
        updated_at: Date.now(),
        messages: [
          { id: crypto.randomUUID(), role: "user", content: handoff.prompt },
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: handoff.analysis,
            toolCalls: (handoff.tools || []) as Message["toolCalls"],
          },
        ],
      };
      stored = [conv, ...stored];
      saveConversations(stored);
    }
    if (stored.length > 0) {
      setConversations(stored);
      setActiveId(stored[0].id);
      if (stored[0].cluster_id) setClusterId(stored[0].cluster_id);
    }

    let cancelled = false;
    listChatSessions()
      .then(async (summaries) => {
        if (cancelled) return;
        const remoteById = new Map(summaries.map((s) => [s.session_id, s]));
        const localById = new Map(stored.map((c) => [c.id, c]));
        const allIds = new Set([...remoteById.keys(), ...localById.keys()]);
        const merged: Conversation[] = [];
        const toPush: Conversation[] = [];
        for (const id of allIds) {
          const local = localById.get(id);
          const remote = remoteById.get(id);
          if (remote && (!local || remote.updated_at >= local.updated_at)) {
            // Server has newer (or only) version — render a stub now and let
            // selection lazy-load full messages.
            merged.push({
              id: remote.session_id,
              title: remote.title || "Untitled",
              cluster_id: remote.cluster_id || "",
              updated_at: remote.updated_at,
              messages: local?.messages || [],
            });
          } else if (local) {
            merged.push(local);
            // Local has newer version (or remote missing) — push it up.
            if (!remote || local.updated_at > remote.updated_at) {
              toPush.push(local);
            }
          }
        }
        merged.sort((a, b) => b.updated_at - a.updated_at);
        setConversations(merged);
        saveConversations(merged);
        if (merged.length > 0 && !activeId) {
          setActiveId(merged[0].id);
          if (merged[0].cluster_id && !deepLinkClusterRef.current)
            setClusterId(merged[0].cluster_id);
        }
        // Push local-only sessions to DDB so a different device sees them.
        // Fire-and-forget; failures don't block the UI.
        for (const conv of toPush) {
          putChatSession(conv.id, {
            title: conv.title,
            cluster_id: conv.cluster_id,
            messages: conv.messages,
          }).catch((e) => console.warn("[chat] push local→server failed", e));
        }
      })
      .catch((e) => {
        console.warn(
          "[chat] listChatSessions failed; staying on localStorage",
          e,
        );
      });
    return () => {
      cancelled = true;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    fetchClusters()
      .then((rows: ClusterRow[]) => {
        setClusters(rows);
        if (rows.length > 0 && !clusterId && !deepLinkClusterRef.current)
          setClusterId(rows[0].cluster_id);
      })
      .catch((e) => console.error("Failed to load clusters:", e));
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const sp = new URLSearchParams(window.location.search);
    const prompt = sp.get("prompt");
    const cluster = sp.get("cluster");
    if (cluster) {
      // Pin + select the deep-linked cluster so the prefilled prompt (and the
      // clusterId sent to the agent) targets the cluster the operator was
      // looking at, not whatever happens to load first.
      deepLinkClusterRef.current = cluster;
      setClusterId(cluster);
    }
    if (prompt) setInput(prompt);
    if (prompt || cluster) {
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  const active = conversations.find((c) => c.id === activeId);
  const messages = active?.messages || [];

  // Cross-device sync: debounce server PUTs by 1.5s so a streaming response
  // (which mutates messages on every token) doesn't fire N writes per turn.
  // Keyed by conversation id — switching conversations cancels the pending
  // write for the previous one.
  const syncTimerRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map(),
  );
  const scheduleSync = useCallback((conv: Conversation) => {
    const existing = syncTimerRef.current.get(conv.id);
    if (existing) clearTimeout(existing);
    const t = setTimeout(() => {
      syncTimerRef.current.delete(conv.id);
      putChatSession(conv.id, {
        title: conv.title,
        cluster_id: conv.cluster_id,
        messages: conv.messages,
      }).catch((e) =>
        console.warn(
          "[chat] putChatSession failed; localStorage stays authoritative",
          e,
        ),
      );
    }, 1500);
    syncTimerRef.current.set(conv.id, t);
  }, []);

  const persist = useCallback(
    (updater: (prev: Conversation[]) => Conversation[]) => {
      setConversations((prev) => {
        const next = updater(prev);
        saveConversations(next);
        // Schedule a debounced server sync per changed conversation.
        // Comparing by reference identifies which conversation actually
        // mutated; skipping unchanged rows keeps the network quiet.
        const prevById = new Map(prev.map((c) => [c.id, c]));
        for (const conv of next) {
          if (prevById.get(conv.id) !== conv) {
            scheduleSync(conv);
          }
        }
        return next;
      });
    },
    [scheduleSync],
  );

  // Lazy detail load when the user switches to a stub conversation that
  // came back from listChatSessions without messages.
  useEffect(() => {
    if (!activeId) return;
    const conv = conversations.find((c) => c.id === activeId);
    if (!conv) return;
    if (conv.messages.length > 0) return;
    let cancelled = false;
    fetchChatSession(activeId)
      .then((detail) => {
        if (cancelled) return;
        const restored = (detail.messages || []).map((m, i) => ({
          id: `${activeId}-${i}`,
          role: m.role as Message["role"],
          content: m.content,
          toolCalls: (m.tool_calls as Message["toolCalls"]) || [],
        }));
        setConversations((prev) =>
          prev.map((c) =>
            c.id === activeId ? { ...c, messages: restored } : c,
          ),
        );
      })
      .catch((e) => console.warn("[chat] fetchChatSession failed", e));
    return () => {
      cancelled = true;
    };
  }, [activeId, conversations]);

  const startNewConversation = useCallback(() => {
    const conv = newConversation(clusterId);
    persist((prev) => [conv, ...prev]);
    setActiveId(conv.id);
    setInput("");
  }, [clusterId, persist]);

  const removeConversation = useCallback(
    (id: string) => {
      persist((prev) => prev.filter((c) => c.id !== id));
      // Best-effort server delete. A failure here just means the row
      // sticks around in DDB until TTL expires it; local UI is already
      // consistent.
      deleteChatSession(id).catch((e) =>
        console.warn("[chat] deleteChatSession failed; will expire via TTL", e),
      );
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
            .filter(
              (q): q is string => typeof q === "string" && q.trim().length > 0,
            )
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

      const userMsg: Message = {
        id: crypto.randomUUID(),
        role: "user",
        content: userText,
      };
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
                msgs[msgs.length - 1] = {
                  ...last,
                  content: last.content + token,
                };
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
                  toolCalls[existing] = {
                    name,
                    status: status as "running" | "done",
                  };
                } else {
                  toolCalls.push({
                    name,
                    status: status as "running" | "done",
                  });
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
                  if (
                    window.confirm(
                      `Delete all ${conversations.length} conversations? This cannot be undone.`,
                    )
                  ) {
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
                      <span className="text-[10px] text-zinc-600">
                        {relTime(c.updated_at)}
                      </span>
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
                Claude{" "}
                {availableModels.find((m) => m.id === modelId)?.label ||
                  "(custom)"}
              </span>
            </div>
            <div className="text-sm text-zinc-200 mt-0.5 truncate">
              {active ? active.title : "Start a new conversation"}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">
              model
            </label>
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
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">
              cluster
            </label>
            {/* Searchable — a native <select> of 100+ clusters is unscannable. */}
            <SearchableClusterSelect
              value={clusterId}
              onChange={setClusterId}
              clusters={clusters}
              placeholder="no clusters"
              className="w-56"
            />
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
                    if (!active) return;
                    exportConversationToPdf(active);
                  }}
                  className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-amber-500/40 hover:text-amber-300 transition-colors"
                  title="Save the entire conversation as PDF — opens the browser print dialog with only the chat content (no app chrome)."
                >
                  🖨 pdf
                </button>
                <button
                  onClick={() => {
                    if (!active) return;
                    if (
                      window.confirm("Clear all messages in this conversation?")
                    ) {
                      persist((prev) =>
                        prev.map((c) =>
                          c.id === active.id
                            ? {
                                ...c,
                                messages: [],
                                title: "New conversation",
                                updated_at: Date.now(),
                              }
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
                자연어로 Aurora 운영을 위임하세요. agent가 MCP 툴로
                메트릭/스키마/EXPLAIN을 호출합니다.
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
              onSaveAsRunbook={(assistant, question) => {
                const titleSeed = (question || assistant.content)
                  .replace(/\s+/g, " ")
                  .trim()
                  .slice(0, 80);
                const body = question
                  ? `## 질문\n${question}\n\n## 진단 + 조치\n${assistant.content}`
                  : assistant.content;
                setRunbookSaveError(null);
                setRunbookDraft({
                  title: titleSeed,
                  body_md: body,
                  tags_csv: "",
                  cluster_id: clusterId || "",
                });
              }}
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
              onKeyDown={(e) =>
                e.key === "Enter" && !e.shiftKey && handleSend()
              }
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

      {runbookDraft && (
        <div
          className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6"
          onClick={() => {
            if (!runbookSaving) setRunbookDraft(null);
          }}
        >
          <div
            className="bg-zinc-950 border border-zinc-800 max-w-2xl w-full max-h-[85vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="px-5 py-4 border-b border-zinc-800 flex items-baseline justify-between">
              <div className="text-base text-zinc-100 font-semibold">
                Runbook으로 저장
              </div>
              <button
                type="button"
                onClick={() => !runbookSaving && setRunbookDraft(null)}
                className="text-zinc-500 hover:text-zinc-200 text-xs"
              >
                ✕ 닫기
              </button>
            </div>
            <div className="px-5 py-4 space-y-3 overflow-y-auto flex-1">
              <label className="block">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  제목
                </div>
                <input
                  value={runbookDraft.title}
                  onChange={(e) =>
                    setRunbookDraft({ ...runbookDraft, title: e.target.value })
                  }
                  className="w-full bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5"
                />
              </label>
              <label className="block">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  본문 (Markdown — 자동 채워짐, 편집 가능)
                </div>
                <textarea
                  value={runbookDraft.body_md}
                  onChange={(e) =>
                    setRunbookDraft({
                      ...runbookDraft,
                      body_md: e.target.value,
                    })
                  }
                  rows={12}
                  className="w-full bg-zinc-900 border border-zinc-700 text-zinc-200 text-xs px-3 py-2 font-mono resize-y"
                />
              </label>
              <label className="block">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  태그 (콤마 구분)
                </div>
                <input
                  value={runbookDraft.tags_csv}
                  onChange={(e) =>
                    setRunbookDraft({
                      ...runbookDraft,
                      tags_csv: e.target.value,
                    })
                  }
                  placeholder="high-cpu, autovacuum"
                  className="w-full bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5 font-mono"
                />
              </label>
              {runbookSaveError && (
                <div className="text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
                  {runbookSaveError}
                </div>
              )}
            </div>
            <div className="px-5 py-3 border-t border-zinc-800 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => !runbookSaving && setRunbookDraft(null)}
                disabled={runbookSaving}
                className="text-xs text-zinc-400 px-3 py-1.5"
              >
                취소
              </button>
              <button
                type="button"
                disabled={runbookSaving || !runbookDraft.title.trim()}
                onClick={async () => {
                  setRunbookSaving(true);
                  setRunbookSaveError(null);
                  try {
                    const tags = runbookDraft.tags_csv
                      .split(",")
                      .map((t) => t.trim())
                      .filter(Boolean);
                    await createRunbook({
                      cluster_id: runbookDraft.cluster_id || undefined,
                      title: runbookDraft.title.trim(),
                      body_md: runbookDraft.body_md,
                      tags,
                      source: "chat",
                      source_ref: activeId || undefined,
                    });
                    setRunbookDraft(null);
                    setRunbookSavedToast(Date.now());
                    setTimeout(() => setRunbookSavedToast(null), 2500);
                  } catch (e) {
                    setRunbookSaveError(
                      e instanceof Error ? e.message : "save failed",
                    );
                  } finally {
                    setRunbookSaving(false);
                  }
                }}
                className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors"
              >
                {runbookSaving ? "저장 중…" : "Runbook 저장"}
              </button>
            </div>
          </div>
        </div>
      )}

      {runbookSavedToast && (
        <div className="fixed bottom-6 right-6 z-50 bg-emerald-500/15 border border-emerald-500/40 text-emerald-200 px-4 py-2 text-sm shadow-lg">
          ✓ Runbook으로 저장됨.{" "}
          <a
            href="/runbooks"
            className="text-amber-300 hover:text-amber-200 underline underline-offset-2 ml-1"
          >
            보기 →
          </a>
        </div>
      )}
    </div>
  );
}
