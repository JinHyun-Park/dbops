"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ToolStatus } from "./tool-status";

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  toolCalls?: { name: string; status: "running" | "done" }[];
  followups?: string[];
}

interface MessageListProps {
  messages: Message[];
  onFollowupClick?: (text: string) => void;
  followupsLoading?: boolean;
  onSaveAsRunbook?: (assistant: Message, question: string | null) => void;
}

export function MessageList({
  messages,
  onFollowupClick,
  followupsLoading,
  onSaveAsRunbook,
}: MessageListProps) {
  const lastIdx = messages.length - 1;
  return (
    <div className="flex flex-col gap-4 p-4">
      {messages.map((msg, idx) => {
        const isLast = idx === lastIdx;
        const showFollowups =
          msg.role === "assistant" &&
          (msg.followups?.length || followupsLoading) &&
          isLast;
        return (
          <div
            key={msg.id}
            className={`flex flex-col ${
              msg.role === "user" ? "items-end" : "items-start"
            }`}
          >
            <div
              className={`max-w-[80%] rounded-lg px-4 py-3 shadow-sm ${
                msg.role === "user"
                  ? "bg-blue-600 text-white"
                  : "bg-zinc-800 text-zinc-100 border border-zinc-700/70"
              }`}
            >
              {msg.toolCalls && msg.toolCalls.length > 0 && (
                <div className="flex flex-wrap gap-2 mb-2">
                  {msg.toolCalls.map((tc, i) => (
                    <ToolStatus key={i} name={tc.name} status={tc.status} />
                  ))}
                </div>
              )}
              {msg.role === "user" ? (
                <div className="whitespace-pre-wrap">{msg.content}</div>
              ) : (
                <div
                  className="prose prose-invert prose-sm max-w-none
                  prose-headings:text-zinc-100 prose-headings:font-semibold prose-headings:mt-3 prose-headings:mb-2
                  prose-p:my-2 prose-p:leading-relaxed
                  prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5
                  prose-strong:text-zinc-100
                  prose-code:text-emerald-300 prose-code:bg-zinc-900 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:before:content-none prose-code:after:content-none
                  prose-pre:bg-zinc-900 prose-pre:border prose-pre:border-zinc-700 prose-pre:my-3
                  prose-a:text-sky-300 hover:prose-a:text-emerald-300
                  prose-table:my-3 prose-th:border prose-th:border-zinc-700 prose-th:bg-zinc-900 prose-th:px-3 prose-th:py-2
                  prose-td:border prose-td:border-zinc-700 prose-td:px-3 prose-td:py-2
                  prose-blockquote:border-l-emerald-500 prose-blockquote:text-zinc-300
                  prose-hr:border-zinc-700"
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {msg.content}
                  </ReactMarkdown>
                </div>
              )}
            </div>

            {msg.role === "assistant" && extractApprovalId(msg.content) && (
              <ApprovalCallout approvalId={extractApprovalId(msg.content)!} />
            )}

            {msg.role === "assistant" &&
              onSaveAsRunbook &&
              msg.content.length > 100 && (
                <button
                  type="button"
                  onClick={() => {
                    // Walk back to the immediately preceding user message —
                    // that's the question this assistant turn answered.
                    let q: string | null = null;
                    for (let i = idx - 1; i >= 0; i--) {
                      if (messages[i].role === "user") {
                        q = messages[i].content;
                        break;
                      }
                    }
                    onSaveAsRunbook(msg, q);
                  }}
                  className="mt-1.5 text-[10px] text-zinc-500 hover:text-amber-300 transition-colors px-1"
                  title="이 진단을 Runbook으로 저장 — 같은 패턴 재발 시 곧바로 참조"
                >
                  ✓ Runbook 저장
                </button>
              )}

            {showFollowups && (
              <div className="mt-2 flex flex-col gap-1.5 items-start max-w-[80%]">
                <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 px-1">
                  suggested next
                </div>
                {msg.followups && msg.followups.length > 0 ? (
                  <div className="flex flex-wrap gap-1.5">
                    {msg.followups.map((q, i) => (
                      <button
                        key={i}
                        onClick={() => onFollowupClick?.(q)}
                        className="text-left text-xs px-2.5 py-1.5 border border-zinc-700 text-zinc-300 hover:border-amber-500/50 hover:text-amber-300 hover:bg-amber-500/5 transition-colors"
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="text-[10px] text-zinc-600 italic px-1">
                    generating follow-up suggestions…
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// UUID v4 pattern — matches what the request_approval tool returns.
const APPROVAL_ID_RX =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i;

function extractApprovalId(content: string): string | null {
  // Heuristic: the agent's message must mention an approval to surface
  // the callout, not just any UUID it happened to print.
  if (!/approval/i.test(content)) return null;
  const m = content.match(APPROVAL_ID_RX);
  return m ? m[0] : null;
}

function ApprovalCallout({ approvalId }: { approvalId: string }) {
  return (
    <div className="mt-2 max-w-[80%] border-l-2 border-amber-500/60 bg-amber-500/5 px-3 py-2 flex items-start gap-2">
      <span className="text-amber-300 mt-0.5">🔔</span>
      <div className="flex-1 text-xs">
        <div className="text-amber-200 font-medium">
          DBA 승인이 등록되었습니다
        </div>
        <div className="text-zinc-400 mt-0.5 font-mono break-all">
          {approvalId}
        </div>
        <a
          href="/approvals"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-block mt-1.5 text-amber-300 hover:text-amber-200 underline underline-offset-2"
        >
          Approval Center 열기 →
        </a>
      </div>
    </div>
  );
}
