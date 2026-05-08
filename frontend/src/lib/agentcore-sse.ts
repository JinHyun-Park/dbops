import { getToken } from "./auth";

const RUNTIME_URL = process.env.NEXT_PUBLIC_AGENTCORE_RUNTIME_URL || "";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  toolCalls?: { name: string; status: "running" | "done"; result?: string }[];
}

export function streamChat(
  message: string,
  clusterId: string,
  onToken: (token: string) => void,
  onToolCall: (name: string, status: string) => void,
  onDone: () => void,
  onError: (error: Error) => void,
): AbortController {
  const controller = new AbortController();
  const token = getToken();

  fetch(`${RUNTIME_URL}/invoke`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ message, context: { cluster_id: clusterId } }),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) throw new Error(`SSE failed: ${response.status}`);
      const reader = response.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const text = decoder.decode(value, { stream: true });
        const lines = text.split("\n");
        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const data = line.slice(6);
            if (data === "[DONE]") { onDone(); return; }
            try {
              const parsed = JSON.parse(data);
              if (parsed.type === "text") onToken(parsed.content);
              if (parsed.type === "tool_use") onToolCall(parsed.name, parsed.status);
            } catch {
              onToken(data);
            }
          }
        }
      }
      onDone();
    })
    .catch((err) => {
      if (err.name !== "AbortError") onError(err);
    });

  return controller;
}
