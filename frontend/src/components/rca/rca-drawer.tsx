"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { X, Sparkles, RefreshCw } from "lucide-react";
import { streamChat } from "@/lib/agentcore-sse";
import { RCA_PROMPT } from "@/lib/rca-link";
import { stashRcaHandoff } from "@/lib/rca-handoff";
import { loadRcaCache, saveRcaCache } from "@/lib/rca-cache";
import { prettyToolName } from "@/lib/tool-name";

// Korean relative time for the "cached analysis" banner.
function koAgo(ts: number): string {
  const ms = Date.now() - ts;
  if (ms < 60_000) return "방금";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}분 전`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}시간 전`;
  return `${Math.floor(ms / 86_400_000)}일 전`;
}

// RCA runs IN PLACE: a right-side drawer that streams the agent's root-cause
// analysis without leaving the page and — crucially — without writing into the
// user's chat history. It reuses the same agent SSE (streamChat) as the chat,
// but on a throwaway session id, so the real diagnose_root_cause engine runs
// and nothing pollutes saved conversations.

interface RcaContextValue {
  open: (clusterId: string) => void;
}
const RcaContext = createContext<RcaContextValue | null>(null);

export function useRca(): RcaContextValue {
  const ctx = useContext(RcaContext);
  // A no-op fallback keeps RcaButton usable even if a tree forgot the provider.
  return ctx ?? { open: () => {} };
}

interface ToolCall {
  name: string;
  status: string;
}

export function RcaProvider({ children }: { children: React.ReactNode }) {
  const [cluster, setCluster] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [tools, setTools] = useState<ToolCall[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // When the drawer shows a previously-cached analysis (no live run), this holds
  // the completion time of that analysis; null while streaming a fresh run.
  const [cachedTs, setCachedTs] = useState<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const runSeq = useRef(0);
  // Mirror the streamed text/tools into refs so the on-complete callback can
  // snapshot the FINAL values into the cache (state is stale inside the closure).
  const accRef = useRef("");
  const toolsRef = useRef<ToolCall[]>([]);
  const router = useRouter();

  const run = useCallback((clusterId: string) => {
    abortRef.current?.abort();
    const seq = ++runSeq.current;
    setText("");
    setTools([]);
    setError(null);
    setStreaming(true);
    setCachedTs(null); // this is a live run, not a cached view
    accRef.current = "";
    toolsRef.current = [];
    abortRef.current = streamChat(
      RCA_PROMPT,
      clusterId,
      (token) => {
        if (runSeq.current === seq) {
          accRef.current += token;
          setText((t) => t + token);
        }
      },
      (name, status) => {
        if (runSeq.current !== seq) return;
        const i = toolsRef.current.findIndex((t) => t.name === name);
        if (i >= 0) toolsRef.current[i] = { name, status };
        else toolsRef.current = [...toolsRef.current, { name, status }];
        setTools((prev) => {
          const j = prev.findIndex((t) => t.name === name);
          if (j >= 0) {
            const next = [...prev];
            next[j] = { name, status };
            return next;
          }
          return [...prev, { name, status }];
        });
      },
      () => {
        if (runSeq.current === seq) {
          setStreaming(false);
          // Persist the completed analysis so reopening this cluster's drawer
          // shows it instantly (no re-run). Snapshot from refs (state is stale).
          if (accRef.current.trim()) {
            saveRcaCache(clusterId, {
              analysis: accRef.current,
              tools: toolsRef.current,
              ts: Date.now(),
            });
          }
        }
      },
      (err) => {
        if (runSeq.current === seq) {
          setError(err?.message || "분석 중 오류가 발생했습니다");
          setStreaming(false);
        }
      },
      // Throwaway session — keeps the agent's chat memory + saved conversations
      // clean. (chat sessions are dbops-session-*; this is rca-*.)
      `rca-${clusterId}-${seq}`,
    );
  }, []);

  const open = useCallback(
    (clusterId: string) => {
      setCluster(clusterId);
      // Show the last completed analysis instantly if we have one — no agent
      // call, no waiting. "다시 실행" re-runs for a fresh one.
      const cached = loadRcaCache(clusterId);
      if (cached) {
        abortRef.current?.abort();
        runSeq.current++; // invalidate any in-flight callbacks
        setText(cached.analysis);
        setTools(cached.tools || []);
        setError(null);
        setStreaming(false);
        setCachedTs(cached.ts);
      } else {
        setCachedTs(null);
        run(clusterId);
      }
    },
    [run],
  );

  const close = useCallback(() => {
    abortRef.current?.abort();
    runSeq.current++; // invalidate any in-flight callbacks
    setCluster(null);
    setStreaming(false);
  }, []);

  // Esc closes the drawer.
  useEffect(() => {
    if (!cluster) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [cluster, close]);

  return (
    <RcaContext.Provider value={{ open }}>
      {children}
      {cluster && (
        <div className="fixed inset-0 z-50 flex justify-end">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={close}
          />
          <div className="relative w-full max-w-xl h-full bg-zinc-950 border-l border-zinc-800 shadow-2xl flex flex-col">
            {/* Header */}
            <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-zinc-800">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-sm font-medium text-zinc-100">
                  <Sparkles size={15} className="text-amber-300" />
                  근본 원인 분석
                </div>
                <div className="text-[11px] font-mono text-zinc-500 mt-0.5 truncate">
                  {cluster}
                </div>
              </div>
              <button
                onClick={close}
                className="text-zinc-500 hover:text-zinc-200 transition-colors"
                title="닫기 (Esc)"
              >
                <X size={18} />
              </button>
            </div>

            {/* Tool calls — proof the real diagnose_root_cause engine ran. */}
            {tools.length > 0 && (
              <div className="px-5 py-2 border-b border-zinc-800/60 flex flex-wrap gap-1.5">
                {tools.map((t) => (
                  <span
                    key={t.name}
                    className={`text-[10px] font-mono px-1.5 py-0.5 border ${
                      t.status === "done"
                        ? "border-emerald-500/40 text-emerald-300/80"
                        : "border-amber-500/40 text-amber-300/80"
                    }`}
                  >
                    {prettyToolName(t.name)} {t.status === "done" ? "✓" : "…"}
                  </span>
                ))}
              </div>
            )}

            {/* Body */}
            <div className="flex-1 overflow-y-auto px-5 py-4">
              {cachedTs && !streaming && (
                <div className="mb-3 flex items-start gap-2 px-3 py-2 border border-zinc-700/60 bg-zinc-900/60 text-[11px] text-zinc-400">
                  <span className="text-zinc-500">🕘</span>
                  <span>
                    {koAgo(cachedTs)} 저장된 분석입니다 — 재분석 없이 다시 보는
                    중. 최신 상태가 필요하면 아래 “다시 실행”을 누르세요.
                  </span>
                </div>
              )}
              {error && (
                <div className="mb-3 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
                  {error}
                </div>
              )}
              {text ? (
                <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-zinc-900 prose-pre:border prose-pre:border-zinc-800">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {text}
                  </ReactMarkdown>
                </div>
              ) : (
                streaming && (
                  <div className="text-sm text-zinc-500">
                    최근 신호를 상관분석하는 중…
                  </div>
                )
              )}
              {streaming && text && (
                <span className="inline-block w-1.5 h-4 ml-0.5 bg-amber-400/80 animate-pulse align-text-bottom" />
              )}
            </div>

            {/* Footer */}
            <div className="px-5 py-3 border-t border-zinc-800 flex items-center justify-between gap-2">
              <button
                onClick={() => cluster && run(cluster)}
                disabled={streaming}
                className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-amber-500/50 hover:text-amber-200 disabled:opacity-50 transition-colors"
              >
                <RefreshCw size={12} />
                다시 실행
              </button>
              <button
                onClick={() => {
                  if (!cluster) return;
                  // Carry the question AND the streamed analysis into a fresh
                  // chat conversation — so it's preserved + continuable, not
                  // discarded into whatever thread was last open.
                  abortRef.current?.abort();
                  runSeq.current++;
                  stashRcaHandoff({
                    cluster_id: cluster,
                    prompt: RCA_PROMPT,
                    analysis: text,
                    tools,
                  });
                  setCluster(null);
                  router.push("/chat");
                }}
                disabled={!text}
                className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-emerald-500/50 hover:text-emerald-200 disabled:opacity-50 transition-colors"
              >
                전체 대화로 이어가기 →
              </button>
            </div>
          </div>
        </div>
      )}
    </RcaContext.Provider>
  );
}
