import { getAccessToken } from "./auth";

const RUNTIME_ARN = process.env.NEXT_PUBLIC_AGENTCORE_RUNTIME_ARN
  || "arn:aws:bedrock-agentcore:ap-northeast-2:830858425797:runtime/dbops_dev_runtime-fKdtxg4wAc";
const REGION = process.env.NEXT_PUBLIC_AWS_REGION || "ap-northeast-2";
const QUALIFIER = "DEFAULT";

function buildInvokeUrl(): string {
  const escapedArn = encodeURIComponent(RUNTIME_ARN);
  return `https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${escapedArn}/invocations?qualifier=${QUALIFIER}`;
}

function genSessionId(): string {
  const stored = sessionStorage.getItem("dbops_session_id");
  if (stored && stored.length >= 33) return stored;
  const id = `dbops-session-${crypto.randomUUID()}`;
  sessionStorage.setItem("dbops_session_id", id);
  return id;
}

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
  const token = getAccessToken();

  if (!token) {
    onError(new Error("Not authenticated"));
    return controller;
  }

  const sessionId = genSessionId();
  const promptText = clusterId && clusterId !== "default-cluster"
    ? `[cluster: ${clusterId}]\n${message}`
    : message;

  fetch(buildInvokeUrl(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream, application/json",
      Authorization: `Bearer ${token}`,
      "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sessionId,
    },
    body: JSON.stringify({ prompt: promptText }),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        const txt = await response.text().catch(() => "");
        throw new Error(`HTTP ${response.status}: ${txt.slice(0, 200)}`);
      }
      const reader = response.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.trim()) continue;
          if (line.startsWith("data: ")) {
            const data = line.slice(6).trim();
            if (data === "[DONE]") { onDone(); return; }
            try {
              const parsed = JSON.parse(data);
              if (typeof parsed === "string") {
                onToken(parsed);
              } else if (parsed.type === "text" || parsed.type === "content_block_delta") {
                onToken(parsed.content || parsed.delta?.text || "");
              } else if (parsed.type === "tool_use") {
                onToolCall(parsed.name || "tool", parsed.status || "running");
              } else if (parsed.data) {
                onToken(typeof parsed.data === "string" ? parsed.data : JSON.stringify(parsed.data));
              }
            } catch {
              onToken(data);
            }
          } else {
            onToken(line);
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
