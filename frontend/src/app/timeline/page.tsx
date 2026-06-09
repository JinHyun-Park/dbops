"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchTimeline,
  type TimelineItem,
  type TimelineResponse,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  EmptyState,
} from "@/components/design-system/page-shell";
import { useSelectedCluster } from "@/lib/use-selected-cluster";
import { ClusterPicker } from "@/components/design-system/cluster-picker";

const WINDOWS: { label: string; hours: number }[] = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 24 * 7 },
];

// Category presentation — color the dot + chip by signal class so the
// DBA can scan a long list and spot the "writes" (audit/schema_change)
// vs the "noise" (proactive/ack) at a glance.
const CATEGORY_STYLE: Record<
  string,
  { label: string; dot: string; chip: string }
> = {
  alert: {
    label: "alert",
    dot: "bg-rose-400",
    chip: "bg-rose-500/10 text-rose-300 border-rose-500/40",
  },
  rds_event: {
    label: "RDS event",
    dot: "bg-amber-400",
    chip: "bg-amber-500/10 text-amber-300 border-amber-500/40",
  },
  proactive: {
    label: "proactive",
    dot: "bg-sky-400",
    chip: "bg-sky-500/10 text-sky-300 border-sky-500/40",
  },
  ack: {
    label: "ack",
    dot: "bg-emerald-400",
    chip: "bg-emerald-500/10 text-emerald-300 border-emerald-500/40",
  },
  schema_change: {
    label: "schema",
    dot: "bg-purple-400",
    chip: "bg-purple-500/15 text-purple-300 border-purple-500/40",
  },
  audit: {
    label: "audit",
    dot: "bg-zinc-300",
    chip: "bg-zinc-700 text-zinc-200 border-zinc-600",
  },
};

function categoryStyle(category: string) {
  return (
    CATEGORY_STYLE[category] || {
      label: category,
      dot: "bg-zinc-500",
      chip: "bg-zinc-700/40 text-zinc-300 border-zinc-700",
    }
  );
}

function relTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

export default function TimelinePage() {
  // Global cluster selection (shared store) — stays in sync with ⌘K / header.
  const { selected: clusterId } = useSelectedCluster();
  const [hours, setHours] = useState<number>(24);
  const [data, setData] = useState<TimelineResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Empty Set = show all. A category in the set = filtered IN.
  const [filterIn, setFilterIn] = useState<Set<string>>(new Set());

  const load = useCallback(() => {
    if (!clusterId) return;
    setLoading(true);
    setError(null);
    fetchTimeline(clusterId, { hours })
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [clusterId, hours]);

  useEffect(() => {
    load();
  }, [load]);

  // Filter chips: show every category that the response actually contains
  // so empty buckets don't render as dead UI.
  const presentCategories = useMemo(() => data?.categories || [], [data]);

  const visibleItems = useMemo(() => {
    if (!data) return [];
    if (filterIn.size === 0) return data.items;
    return data.items.filter((i) => filterIn.has(i.category));
  }, [data, filterIn]);

  return (
    <PageBody>
      <PageHeader
        eyebrow="incident"
        title="Timeline"
        description="단일 cluster의 모든 운영 신호를 시간축 한 줄에. 알림 발화, RDS 이벤트, 스키마 변경, 실행된 쓰기 작업이 모두 같은 흐름에 보입니다. 사고 시점 컨텍스트를 한 화면에 잡아두는 용도."
        actions={
          <div className="flex items-center gap-2">
            <ClusterPicker selected={clusterId} />
            <div className="flex border border-zinc-800">
              {WINDOWS.map((w) => (
                <button
                  key={w.hours}
                  onClick={() => setHours(w.hours)}
                  className={`text-xs px-3 py-1.5 transition-colors ${
                    hours === w.hours
                      ? "bg-zinc-100 text-zinc-950"
                      : "text-zinc-400 hover:text-zinc-100"
                  }`}
                >
                  {w.label}
                </button>
              ))}
            </div>
            {/* Deep-link into workload diff for this cluster — the
                natural next question after "what happened?" is "what
                did it do to the query workload?". */}
            {clusterId && (
              <a
                href={`/workload-diff?cluster=${encodeURIComponent(clusterId)}`}
                className="text-xs px-3 py-1.5 border border-zinc-800 text-zinc-400 hover:border-amber-500/60 hover:text-amber-200 transition-colors"
              >
                워크로드 비교 →
              </a>
            )}
          </div>
        }
      />

      {error && (
        <div className="mb-4 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {error}
        </div>
      )}

      {/* Category filter chips */}
      {presentCategories.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 mb-5">
          <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-1">
            filter
          </span>
          {presentCategories.map((c) => {
            const s = categoryStyle(c);
            const active = filterIn.size === 0 || filterIn.has(c);
            return (
              <button
                key={c}
                onClick={() => {
                  setFilterIn((prev) => {
                    const next = new Set(prev);
                    if (next.has(c)) next.delete(c);
                    else next.add(c);
                    return next;
                  });
                }}
                className={`text-[11px] px-2 py-1 border transition-opacity ${
                  s.chip
                } ${active ? "opacity-100" : "opacity-30"}`}
              >
                {s.label}
              </button>
            );
          })}
          {filterIn.size > 0 && (
            <button
              onClick={() => setFilterIn(new Set())}
              className="text-[11px] px-2 py-1 border border-zinc-800 text-zinc-500 hover:text-zinc-300"
            >
              clear
            </button>
          )}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-zinc-500">불러오는 중…</div>
      ) : !data || visibleItems.length === 0 ? (
        <EmptyState
          eyebrow="timeline"
          title="이 윈도우에 신호가 없습니다"
          description={
            filterIn.size > 0
              ? "현재 필터에 매칭되는 신호가 없습니다. clear 를 눌러 전체를 보세요."
              : "이 cluster에서 최근 발생한 알림, RDS 이벤트, 스키마 변경, 승인 실행이 없습니다. 윈도우를 늘려보세요."
          }
          secondary={{
            href: `/dashboard?cluster=${clusterId}`,
            label: "Dashboard로",
          }}
        />
      ) : (
        <TimelineList items={visibleItems} />
      )}
    </PageBody>
  );
}

function TimelineList({ items }: { items: TimelineItem[] }) {
  return (
    <div className="relative">
      {/* Vertical rail */}
      <div className="absolute left-2 top-2 bottom-2 w-px bg-zinc-800" />
      <ul className="space-y-3">
        {items.map((item, i) => {
          const s = categoryStyle(item.category);
          const ts = new Date(item.ts);
          return (
            <li
              key={`${item.source_id || i}-${item.ts}`}
              className="pl-7 relative"
            >
              {/* Dot */}
              <span
                className={`absolute left-[3px] top-1.5 w-2.5 h-2.5 rounded-full border border-zinc-950 ${s.dot}`}
                aria-hidden
              />
              <div className="flex items-baseline justify-between gap-3 mb-0.5">
                <div className="flex items-center gap-2 min-w-0">
                  <span
                    className={`text-[10px] font-mono px-1.5 py-0.5 border ${s.chip}`}
                  >
                    {s.label}
                  </span>
                  <span className="text-sm text-zinc-100 truncate">
                    {item.title}
                  </span>
                </div>
                <div className="text-[11px] text-zinc-500 tabular-nums flex-shrink-0">
                  {ts.toLocaleString()} · {relTime(item.ts)}
                </div>
              </div>
              {item.detail && (
                <pre className="text-[11px] text-zinc-400 font-mono whitespace-pre-wrap break-words pl-[2px] mt-1 max-h-32 overflow-y-auto">
                  {item.detail}
                </pre>
              )}
              {item.source && (
                <div className="text-[10px] text-zinc-600 mt-1 font-mono">
                  {item.source}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
