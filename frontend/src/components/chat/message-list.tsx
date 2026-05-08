"use client";

import { ToolStatus } from "./tool-status";

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  toolCalls?: { name: string; status: "running" | "done" }[];
}

interface MessageListProps {
  messages: Message[];
}

export function MessageList({ messages }: MessageListProps) {
  return (
    <div className="flex flex-col gap-4 p-4">
      {messages.map((msg) => (
        <div
          key={msg.id}
          className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
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
            <div className="whitespace-pre-wrap">{msg.content}</div>
          </div>
        </div>
      ))}
    </div>
  );
}
