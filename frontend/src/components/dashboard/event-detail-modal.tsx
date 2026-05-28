"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { streamChat } from "@/lib/agentcore-sse";

export interface DashboardEvent {
  id?: number | string;
  ts: string;
  event_type: string;
  severity: string;
  source?: string;
  message?: string;
  // jsonb arrives as a JSON-encoded string from rds-data; we parse on demand.
  raw_event?: string | Record<string, unknown> | null;
}

interface Props {
  event: DashboardEvent;
  clusterId?: string;
  prettyLabel: string;
  onClose: () => void;
}

function tryParse(raw: DashboardEvent["raw_event"]): unknown {
  if (raw == null) return null;
  if (typeof raw === "object") return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

// Extract the operationally relevant fields from a CloudTrail or RDS native
// event so the modal stays readable without scrolling through 4kB of JSON.
function extractKeyFacts(rawObj: unknown): { label: string; value: string }[] {
  if (!rawObj || typeof rawObj !== "object") return [];
  const r = rawObj as Record<string, unknown>;
  const detail = (r.detail as Record<string, unknown>) || {};
  const req = (detail.requestParameters as Record<string, unknown>) || {};
  const resp = (detail.responseElements as Record<string, unknown>) || {};
  const ident = (detail.userIdentity as Record<string, unknown>) || {};
  const facts: { label: string; value: string }[] = [];
  const push = (label: string, value: unknown) => {
    if (value == null) return;
    const v = typeof value === "string" ? value : JSON.stringify(value);
    if (v && v.length < 240) facts.push({ label, value: v });
  };
  push("Event ID", detail.eventID);
  push("동작", detail.eventName);
  push("클러스터 ID", req.dBClusterIdentifier || resp.dBClusterIdentifier);
  push("인스턴스 ID", req.dBInstanceIdentifier);
  push("스냅샷 ID", req.dBSnapshotIdentifier);
  push(
    "파라미터 그룹",
    req.dBClusterParameterGroupName || req.dBParameterGroupName,
  );
  push("엔진", resp.engine || req.engine);
  push("엔진 버전", resp.engineVersion || req.engineVersion);
  push("에러 코드", detail.errorCode);
  push("에러 메시지", detail.errorMessage);
  push("호출 주체", ident.invokedBy || ident.userName);
  push("소스 IP", detail.sourceIPAddress);
  push("리전", r.region);
  push("이벤트 시각", detail.eventTime || r.time);
  return facts;
}

export function EventDetailModal({
  event,
  clusterId,
  prettyLabel,
  onClose,
}: Props) {
  const [insight, setInsight] = useState("");
  const [insightLoading, setInsightLoading] = useState(false);
  const [insightError, setInsightError] = useState<string | null>(null);
  const [tab, setTab] = useState<"summary" | "raw">("summary");

  const parsed = tryParse(event.raw_event);
  const keyFacts = extractKeyFacts(parsed);
  const sev = (event.severity || "info").toLowerCase();

  const handleAnalyze = () => {
    if (!clusterId) {
      setInsightError(
        "cluster_id를 가져오지 못했어요 — 대시보드를 새로고침하세요",
      );
      return;
    }
    setInsight("");
    setInsightError(null);
    setInsightLoading(true);
    const detailJson = JSON.stringify(parsed ?? {}, null, 2).slice(0, 8000);
    const message =
      `Aurora 클러스터에서 운영 이벤트가 기록됐어. **한국어로** 다음 3개 섹션으로 짧고 명확하게 설명해줘:\n` +
      `1. **무슨 일이 일어났는지** — 한 문장.\n` +
      `2. **영향** — 무엇이 깨지거나 달라질 수 있는지 (1–2문장, 이 클러스터의 런타임 관점에서 구체적으로).\n` +
      `3. **권장 조치** — DBA가 지금 취해야 할 구체적인 행동 한 가지 ` +
      `(영향이 없으면 "조치 불필요"라고 답해줘).\n\n` +
      `Event metadata:\n` +
      `- Cluster: ${clusterId}\n` +
      `- Type: ${event.event_type} (${prettyLabel})\n` +
      `- Severity: ${event.severity}\n` +
      `- Source: ${event.source || "?"}\n` +
      `- Time: ${event.ts}\n` +
      `- Message: ${event.message || "(none)"}\n\n` +
      `Raw event (truncated):\n\`\`\`json\n${detailJson}\n\`\`\``;

    streamChat(
      message,
      clusterId,
      (tok) => setInsight((prev) => prev + tok),
      () => {},
      () => setInsightLoading(false),
      (err) => {
        setInsightError(err.message);
        setInsightLoading(false);
      },
    );
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-zinc-950/80 backdrop-blur flex items-center justify-center p-5"
      onClick={onClose}
    >
      <div
        className="w-full max-w-3xl max-h-[90vh] flex flex-col bg-zinc-900 border border-zinc-700 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between px-5 py-4 border-b border-zinc-800">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <span
                className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${
                  sev === "critical" || sev === "error"
                    ? "bg-rose-500/20 text-rose-300 border border-rose-500/40"
                    : sev === "warning"
                      ? "bg-amber-500/15 text-amber-300 border border-amber-500/40"
                      : "bg-sky-500/15 text-sky-300 border border-sky-500/30"
                }`}
              >
                {sev}
              </span>
              <span className="text-[10px] text-zinc-500 font-mono">
                {event.source || "—"}
              </span>
              <span className="text-[10px] text-zinc-600">·</span>
              <span className="text-[10px] text-zinc-500">{event.ts}</span>
            </div>
            <h2 className="text-lg font-semibold text-zinc-100 truncate">
              {prettyLabel}
            </h2>
            {event.message && (
              <div className="text-xs text-zinc-400 mt-1 leading-snug">
                {event.message}
              </div>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-200 text-xl leading-none ml-3"
            aria-label="닫기"
          >
            ×
          </button>
        </header>

        <div className="flex items-center gap-2 px-5 py-2 border-b border-zinc-800">
          <button
            onClick={() => setTab("summary")}
            className={`text-[10px] uppercase tracking-[0.16em] px-2.5 py-1 border transition-colors ${
              tab === "summary"
                ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
                : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
            }`}
          >
            요약 + AI
          </button>
          <button
            onClick={() => setTab("raw")}
            className={`text-[10px] uppercase tracking-[0.16em] px-2.5 py-1 border transition-colors ${
              tab === "raw"
                ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
                : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
            }`}
          >
            원본 이벤트
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {tab === "summary" ? (
            <>
              {keyFacts.length > 0 && (
                <div className="mb-4">
                  <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 mb-2">
                    주요 정보
                  </div>
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                    {keyFacts.map((f, i) => (
                      <div key={i} className="contents">
                        <dt className="text-zinc-500">{f.label}</dt>
                        <dd className="text-zinc-300 font-mono break-all">
                          {f.value}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}

              <div className="border-t border-zinc-800 pt-3">
                <div className="flex items-center justify-between mb-2">
                  <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
                    AI 분석
                  </div>
                  <button
                    onClick={handleAnalyze}
                    disabled={insightLoading || !clusterId}
                    className="text-xs px-3 py-1 border border-sky-500/40 text-sky-300 hover:bg-sky-500/10 disabled:opacity-50 transition-colors"
                  >
                    {insightLoading
                      ? "분석 중…"
                      : insight
                        ? "다시 분석"
                        : "원인 설명 + 조치"}
                  </button>
                </div>
                {insightError && (
                  <div className="text-xs text-rose-400 border border-rose-500/40 bg-rose-500/10 px-3 py-2 mb-2">
                    {insightError}
                  </div>
                )}
                {!insight && !insightLoading && !insightError && (
                  <div className="text-xs text-zinc-500">
                    <span className="text-sky-300">원인 설명 + 조치</span>{" "}
                    버튼을 누르면 변경 사항, 영향, 권장 조치 한 가지를 받을 수
                    있어요.
                  </div>
                )}
                {insight && (
                  <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-zinc-950 prose-pre:border prose-pre:border-zinc-800 prose-code:text-sky-300">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {insight}
                    </ReactMarkdown>
                  </div>
                )}
              </div>
            </>
          ) : (
            <pre className="text-[11px] font-mono text-zinc-300 whitespace-pre-wrap break-all">
              {JSON.stringify(parsed, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
