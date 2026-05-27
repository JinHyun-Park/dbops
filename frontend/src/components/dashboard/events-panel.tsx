"use client";

import { useState } from "react";
import { EventDetailModal, type DashboardEvent } from "./event-detail-modal";

const SEVERITY_STYLES: Record<string, string> = {
  critical: "border-l-red-500 bg-red-950/30",
  error: "border-l-red-400 bg-red-950/20",
  warning: "border-l-amber-500 bg-amber-950/20",
  info: "border-l-sky-500 bg-sky-950/10",
};

const SEVERITY_BADGES: Record<string, string> = {
  critical: "bg-rose-500/20 text-rose-300 border border-rose-500/40",
  error: "bg-rose-500/15 text-rose-300 border border-rose-500/30",
  warning: "bg-amber-500/15 text-amber-300 border border-amber-500/40",
  info: "bg-sky-500/15 text-sky-300 border border-sky-500/30",
};

// snake_case / CamelCase → "Title Case" — keeps event_type human-readable
// without needing a back-end migration of historical rows.
function prettifyEventType(raw: string | null | undefined): string {
  if (!raw) return "이벤트";
  let s = String(raw).trim();
  if (!s || s === "unknown" || s === "empty") return "기타 (RDS)";
  if (s.startsWith("alarm_")) {
    const v = s.slice("alarm_".length).toUpperCase();
    return v === "OK" ? "CloudWatch alarm OK" : `CloudWatch alarm ${v}`;
  }
  // Split CamelCase ("CreateDBCluster" → "Create D B Cluster") then collapse the
  // single-letter runs ("Create D B Cluster" → "Create DB Cluster").
  s = s
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2");
  s = s.replace(/_/g, " ");
  s = s.replace(/\b(\w) (\w)\b/g, "$1$2"); // collapse adjacent single letters back
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function relTime(iso: string) {
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  return `${Math.floor(h / 24)}일 전`;
}

export function EventsPanel({
  events,
  clusterId,
}: {
  events: DashboardEvent[];
  clusterId?: string;
}) {
  const [active, setActive] = useState<DashboardEvent | null>(null);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="text-xs text-zinc-400 uppercase tracking-wider">
          Recent Events
        </div>
        {events.length > 0 && (
          <div className="text-[10px] text-zinc-600">
            이벤트 클릭 시 상세 + AI 설명
          </div>
        )}
      </div>
      {events.length === 0 ? (
        <div className="text-xs text-zinc-500 py-2">최근 이벤트 없음</div>
      ) : (
        <div className="space-y-2 max-h-96 overflow-y-auto">
          {events.map((e, i) => {
            const sev = (e.severity || "info").toLowerCase();
            const style =
              SEVERITY_STYLES[sev] || "border-l-zinc-500 bg-zinc-900/30";
            const badge = SEVERITY_BADGES[sev] || SEVERITY_BADGES.info;
            const label = prettifyEventType(e.event_type);
            return (
              <button
                key={`${e.ts}-${i}`}
                onClick={() => setActive(e)}
                className={`w-full text-left border-l-2 ${style} pl-3 py-2 pr-2 rounded-r hover:bg-zinc-800/50 transition-colors`}
              >
                <div className="flex items-center gap-2 mb-0.5">
                  <span className="text-xs font-medium text-zinc-200 truncate">
                    {label}
                  </span>
                  <span
                    className={`text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded ${badge}`}
                  >
                    {sev}
                  </span>
                  {e.source && (
                    <span className="text-[9px] text-zinc-600 font-mono ml-auto">
                      {e.source}
                    </span>
                  )}
                  <span className="text-[10px] text-zinc-500 whitespace-nowrap">
                    {relTime(e.ts)}
                  </span>
                </div>
                <div className="text-xs text-zinc-400 leading-snug truncate">
                  {e.message || (
                    <span className="text-zinc-600 italic">
                      메시지 없음 — 클릭해 원본 이벤트 확인
                    </span>
                  )}
                </div>
              </button>
            );
          })}
        </div>
      )}
      {active && (
        <EventDetailModal
          event={active}
          clusterId={clusterId}
          onClose={() => setActive(null)}
          prettyLabel={prettifyEventType(active.event_type)}
        />
      )}
    </div>
  );
}
