import { getValidAccessToken } from "./auth";

const QUALIFIER = "DEFAULT";

interface AgentRuntimeConfig {
  runtimeArn: string;
  region: string;
}

let runtimeConfigPromise: Promise<AgentRuntimeConfig> | null = null;

function loadRuntimeConfig(): Promise<AgentRuntimeConfig> {
  if (runtimeConfigPromise) return runtimeConfigPromise;
  runtimeConfigPromise = (async () => {
    const fallback: AgentRuntimeConfig = {
      runtimeArn: process.env.NEXT_PUBLIC_AGENTCORE_RUNTIME_ARN || "",
      region: process.env.NEXT_PUBLIC_AWS_REGION || "",
    };
    if (typeof window === "undefined") return fallback;
    try {
      const res = await fetch("/config.json", { cache: "no-store" });
      if (res.ok) {
        const cfg = await res.json();
        return {
          runtimeArn: cfg.agentRuntimeArn || fallback.runtimeArn,
          region: cfg.region || fallback.region,
        };
      }
    } catch {
      // fall through
    }
    return fallback;
  })();
  return runtimeConfigPromise;
}

async function buildInvokeUrl(): Promise<string> {
  const cfg = await loadRuntimeConfig();
  if (!cfg.runtimeArn || !cfg.region) {
    throw new Error("AgentCore runtime ARN/region not configured");
  }
  const escapedArn = encodeURIComponent(cfg.runtimeArn);
  return `https://bedrock-agentcore.${cfg.region}.amazonaws.com/runtimes/${escapedArn}/invocations?qualifier=${QUALIFIER}`;
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

// Tool-use marker patterns that occasionally leak from the model's raw
// stream when Strands forwards text chunks during tool invocation. We
// strip them at the SSE boundary so the user never sees `<invoke …>` or
// `<parameter …>` in chat. Holdback retains a trailing partial tag
// (e.g. "<inv") across chunk boundaries.
const TOOL_TAG_PATTERN =
  /<\/?(?:antml:)?(?:function_calls|invoke|parameter|tool_use|thinking)\b[^>]*>/gi;

// Partial-tag detector — hold back chunks that end with a possibly-incomplete
// marker tag. Matches: `<`, `</`, `<inv…`, `</invoke…`, etc. — bounded by
// requiring no `>` between the trailing `<` and end-of-stream. Plain prose
// like "if x < 5" doesn't match (`< 5` has a non-letter after `<` and isn't
// at end-of-string after `<`); "<a href=…>link</a>" doesn't match because
// every `<` is already closed by a `>` before end-of-stream.
const PARTIAL_TAG_RE = /<\/?(?:[afiopt][^>]{0,200})?$/i;

function makeSanitizer(): (chunk: string) => string {
  let holdback = "";
  return (chunk: string) => {
    const text = holdback + chunk;
    const stripped = text.replace(TOOL_TAG_PATTERN, "");
    const tail = stripped.match(PARTIAL_TAG_RE);
    if (tail) {
      holdback = stripped.slice(tail.index!);
      return stripped.slice(0, tail.index!);
    }
    holdback = "";
    return stripped;
  };
}

export function streamChat(
  message: string,
  clusterId: string,
  onToken: (token: string) => void,
  onToolCall: (name: string, status: string) => void,
  onDone: () => void,
  onError: (error: Error) => void,
  explicitSessionId?: string,
  modelId?: string,
): AbortController {
  const controller = new AbortController();
  const sanitize = makeSanitizer();
  const emit = (raw: string) => {
    const clean = sanitize(raw);
    if (clean) onToken(clean);
  };

  const sessionId = explicitSessionId || genSessionId();
  const promptText =
    clusterId && clusterId !== "default-cluster"
      ? `[cluster: ${clusterId}]\n${message}`
      : message;

  // Resolve a non-expired AccessToken (silent refresh if needed) before invoking.
  getValidAccessToken()
    .then((token) => {
      if (!token) {
        // Refresh token also expired or no current Cognito user — bounce to /login.
        if (typeof window !== "undefined") {
          const next = window.location.pathname + window.location.search;
          window.location.replace(`/login?next=${encodeURIComponent(next)}`);
        }
        throw new Error("Session expired — please log in again");
      }
      console.log("[streamChat] start", {
        tokenLen: token.length,
        clusterId,
        sessionIdProvided: !!explicitSessionId,
      });
      return buildInvokeUrl().then((url) => ({ url, token }));
    })
    .then(({ url, token }) => {
      console.log("[streamChat] invoke URL", url);
      return fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream, application/json",
          Authorization: `Bearer ${token}`,
          "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sessionId,
        },
        body: JSON.stringify(
          modelId
            ? { prompt: promptText, model: modelId }
            : { prompt: promptText },
        ),
        signal: controller.signal,
      });
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
            if (data === "[DONE]") {
              onDone();
              return;
            }
            try {
              const parsed = JSON.parse(data);
              if (typeof parsed === "string") {
                emit(parsed);
              } else if (
                parsed.type === "text" ||
                parsed.type === "content_block_delta"
              ) {
                emit(parsed.content || parsed.delta?.text || "");
              } else if (parsed.type === "tool_use") {
                onToolCall(parsed.name || "tool", parsed.status || "running");
              } else if (parsed.data) {
                emit(
                  typeof parsed.data === "string"
                    ? parsed.data
                    : JSON.stringify(parsed.data),
                );
              }
            } catch {
              emit(data);
            }
          } else {
            emit(line);
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
