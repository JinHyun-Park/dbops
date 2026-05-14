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
}

export function MessageList({
  messages,
  onFollowupClick,
  followupsLoading,
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
              className={`max-w-[80%] rounded-lg px-4 py-3 ${
                msg.role === "user"
                  ? "bg-blue-600 text-white"
                  : "bg-zinc-800 text-zinc-100"
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
                  prose-code:text-amber-300 prose-code:bg-zinc-900 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:before:content-none prose-code:after:content-none
                  prose-pre:bg-zinc-900 prose-pre:border prose-pre:border-zinc-700 prose-pre:my-3
                  prose-a:text-blue-400 hover:prose-a:text-blue-300
                  prose-table:my-3 prose-th:border prose-th:border-zinc-700 prose-th:bg-zinc-900 prose-th:px-3 prose-th:py-2
                  prose-td:border prose-td:border-zinc-700 prose-td:px-3 prose-td:py-2
                  prose-blockquote:border-l-blue-500 prose-blockquote:text-zinc-300
                  prose-hr:border-zinc-700"
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {msg.content}
                  </ReactMarkdown>
                </div>
              )}
            </div>

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
