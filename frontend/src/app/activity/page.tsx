"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchActivity,
  fetchAllActivity,
  fetchClusters,
  type ActivityItem,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  EmptyState,
} from "@/components/design-system/page-shell";
import { getSelectedCluster } from "@/lib/selected-cluster";
import { SearchableClusterSelect } from "@/components/design-system/searchable-cluster-select";
import {
  Activity,
  Clock,
  ShieldCheck,
  XCircle,
  CheckCircle2,
  Download,
} from "lucide-react";
import { buildAuditCsv } from "@/lib/audit-export";

// ---------------------------------------------------------------------------
// Status config: color tokens drawn from the existing dark-zinc palette
// Each status has: icon, ring color, node bg, label text, label bg
// ---------------------------------------------------------------------------
const STATUS_CONFIG: Record<
  string,
  {
    label: string;
    icon: React.ComponentType<{
      size?: number;
      className?: string;
      strokeWidth?: number;
    }>;
    nodeRing: string; // border color of the node circle
    nodeBg: string; // bg color of the node circle
    iconColor: string; // icon fill/stroke color
    labelBg: string; // chip background
    labelText: string; // chip text color
    labelBorder: string;
    railAccent: string; // the short accent line segment above this node
  }
> = {
  pending: {
    label: "pending",
    icon: Clock,
    nodeRing: "border-amber-500/60",
    nodeBg: "bg-amber-500/10",
    iconColor: "text-amber-400",
    labelBg: "bg-amber-500/10",
    labelText: "text-amber-300",
    labelBorder: "border-amber-500/40",
    railAccent: "bg-amber-500/40",
  },
  approved: {
    label: "approved",
    icon: CheckCircle2,
    nodeRing: "border-emerald-500/60",
    nodeBg: "bg-emerald-500/10",
    iconColor: "text-emerald-400",
    labelBg: "bg-emerald-500/10",
    labelText: "text-emerald-300",
    labelBorder: "border-emerald-500/40",
    railAccent: "bg-emerald-500/40",
  },
  consumed: {
    label: "executed",
    icon: Activity,
    nodeRing: "border-sky-500/50",
    nodeBg: "bg-sky-500/10",
    iconColor: "text-sky-400",
    labelBg: "bg-sky-500/10",
    labelText: "text-sky-300",
    labelBorder: "border-sky-500/40",
    railAccent: "bg-sky-500/40",
  },
  rejected: {
    label: "rejected",
    icon: XCircle,
    nodeRing: "border-rose-500/50",
    nodeBg: "bg-rose-500/10",
    iconColor: "text-rose-400",
    labelBg: "bg-rose-500/10",
    labelText: "text-rose-300",
    labelBorder: "border-rose-500/40",
    railAccent: "bg-rose-500/30",
  },
};

const STATUS_CONFIG_FALLBACK = {
  label: "unknown",
  icon: ShieldCheck,
  nodeRing: "border-zinc-700",
  nodeBg: "bg-zinc-800/60",
  iconColor: "text-zinc-400",
  labelBg: "bg-zinc-800/60",
  labelText: "text-zinc-300",
  labelBorder: "border-zinc-700",
  railAccent: "bg-zinc-700",
};

// ---------------------------------------------------------------------------
// Human-readable labels for known action_type values
// DBA jargon kept in English per project i18n rules
// ---------------------------------------------------------------------------
const ACTION_LABEL: Record<string, string> = {
  execute_sql: "SQL 실행",
  modify_parameter: "Parameter 수정",
  modify_scaling: "Scaling 조정",
  manage_maintenance: "Maintenance 관리",
  enable_data_api: "Data API 활성화",
  modify_dynamodb_capacity: "DynamoDB Capacity 변경",
  modify_dynamodb_ttl: "DynamoDB TTL 변경",
  enable_dynamodb_pitr: "DynamoDB PITR 변경",
  set_docdb_profiler: "DocumentDB Profiler 설정",
  create_docdb_index: "DocumentDB Index 생성",
};

function actionLabel(type: string): string {
  return ACTION_LABEL[type] ?? type;
}

const ACTION_OPTIONS = [
  "",
  "execute_sql",
  "modify_parameter",
  "modify_scaling",
  "manage_maintenance",
  "enable_data_api",
  "modify_dynamodb_capacity",
  "modify_dynamodb_ttl",
  "enable_dynamodb_pitr",
  "set_docdb_profiler",
  "create_docdb_index",
  "other",
];

interface ClusterRow {
  cluster_id: string;
}

// created_at is a ms-epoch stored as a DDB STRING sort key; consumed_at /
// resolved_at are ISO strings. Coerce both shapes safely.
function toDate(v: string | null | undefined): Date | null {
  if (!v) return null;
  const n = Number(v);
  const d = new Date(Number.isFinite(n) ? n : v);
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtTime(d: Date): string {
  return d.toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function fmtDayLabel(dateKey: string): string {
  if (dateKey === "(no date)") return "날짜 없음";
  // dateKey is sv-SE = YYYY-MM-DD; parse as local date
  const d = new Date(dateKey + "T00:00:00");
  if (Number.isNaN(d.getTime())) return dateKey;
  const today = new Date();
  const todayKey = today.toLocaleDateString("sv-SE");
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const yesterdayKey = yesterday.toLocaleDateString("sv-SE");
  if (dateKey === todayKey) return "오늘";
  if (dateKey === yesterdayKey) return "어제";
  return d.toLocaleDateString("ko-KR", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
  });
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
export default function ActivityPage() {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [clusters, setClusters] = useState<ClusterRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [clusterFilter, setClusterFilter] = useState<string>(
    () => getSelectedCluster() ?? "",
  );
  const [actorFilter, setActorFilter] = useState<string>("");
  const [actionFilter, setActionFilter] = useState<string>("");

  useEffect(() => {
    fetchClusters()
      .then((rows: ClusterRow[]) => setClusters(rows))
      .catch(() => {});
  }, []);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    fetchActivity({
      cluster_id: clusterFilter || undefined,
      actor: actorFilter || undefined,
      action_type: actionFilter || undefined,
      limit: 200,
    })
      .then((r) => setItems(r.items))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [clusterFilter, actorFilter, actionFilter]);

  useEffect(() => {
    load();
  }, [load]);

  // Group by calendar day (sv-SE locale = stable YYYY-MM-DD key in local time)
  const groups = useMemo(() => {
    const m = new Map<string, ActivityItem[]>();
    for (const it of items) {
      const d = toDate(it.created_at);
      const day = d ? d.toLocaleDateString("sv-SE") : "(no date)";
      if (!m.has(day)) m.set(day, []);
      m.get(day)!.push(it);
    }
    return Array.from(m.entries());
  }, [items]);

  return (
    <PageBody>
      <PageHeader
        eyebrow="audit"
        title="Activity log"
        description="DBOps에서 일어난 모든 쓰기 의사결정의 시간순 기록 — 누가 요청했고 누가 승인했고 언제 실행됐는지. 컴플라이언스 감사와 사후 회고 (post-incident retro) 용도."
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            {items.length > 0 && (
              <button
                type="button"
                onClick={async () => {
                  let rows = items;
                  let capped = false;
                  try {
                    const r = await fetchAllActivity({
                      cluster_id: clusterFilter || undefined,
                      actor: actorFilter || undefined,
                      action_type: actionFilter || undefined,
                    });
                    rows = [...r.items].sort((a, b) =>
                      String(b.created_at).localeCompare(String(a.created_at)),
                    );
                    capped = r.capped;
                  } catch {
                    // fall back to the already-loaded in-view rows
                  }
                  const csv = buildAuditCsv(rows);
                  const blob = new Blob([csv], {
                    type: "text/csv;charset=utf-8",
                  });
                  const a = document.createElement("a");
                  a.href = URL.createObjectURL(blob);
                  a.download = `audit-${new Date()
                    .toISOString()
                    .slice(0, 10)}-${rows.length}rows${
                    capped ? "-capped" : ""
                  }.csv`;
                  a.click();
                  URL.revokeObjectURL(a.href);
                }}
                className="inline-flex items-center gap-1.5 bg-zinc-950 border border-zinc-800 text-zinc-300 hover:border-zinc-600 hover:text-zinc-100 text-xs px-3 py-1.5 transition-colors duration-150"
              >
                <Download size={12} strokeWidth={2} aria-hidden="true" />
                CSV 내보내기
              </button>
            )}
            <SearchableClusterSelect
              value={clusterFilter}
              onChange={setClusterFilter}
              clusters={clusters}
              allowAll
              allLabel="모든 cluster"
              className="w-48"
            />
            <select
              value={actionFilter}
              onChange={(e) => setActionFilter(e.target.value)}
              className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-3 py-1.5 focus:outline-none focus:border-amber-500/60"
            >
              {ACTION_OPTIONS.map((a) => (
                <option key={a} value={a}>
                  {a ? actionLabel(a) : "all actions"}
                </option>
              ))}
            </select>
            <input
              type="text"
              value={actorFilter}
              onChange={(e) => setActorFilter(e.target.value)}
              placeholder="actor"
              className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-3 py-1.5 focus:outline-none focus:border-amber-500/60 w-32"
            />
          </div>
        }
      />

      {error && (
        <div className="mb-4 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-zinc-500 py-8">불러오는 중…</div>
      ) : items.length === 0 ? (
        <EmptyState
          eyebrow="activity"
          title="기록된 활동이 없습니다"
          description="아직 DBOps를 통해 실행된 쓰기 작업이 없거나, 현재 필터에 매칭되는 기록이 없습니다."
          secondary={{ href: "/approvals", label: "대기 중인 승인 보기" }}
        />
      ) : (
        <div className="space-y-10">
          {groups.map(([day, rows], groupIdx) => (
            <DayGroup
              key={day}
              dayKey={day}
              rows={rows}
              isFirst={groupIdx === 0}
            />
          ))}
        </div>
      )}
    </PageBody>
  );
}

// ---------------------------------------------------------------------------
// DayGroup: day header + vertical timeline for that day's events
// ---------------------------------------------------------------------------
function DayGroup({
  dayKey,
  rows,
  isFirst,
}: {
  dayKey: string;
  rows: ActivityItem[];
  isFirst: boolean;
}) {
  return (
    <section>
      {/* Day header */}
      <div className="flex items-center gap-3 mb-5">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-zinc-500 select-none">
          {fmtDayLabel(dayKey)}
        </span>
        <span className="text-[10px] text-zinc-700 font-mono">
          {rows.length}건
        </span>
        <div className="flex-1 h-px bg-zinc-800/70" />
      </div>

      {/* Timeline column */}
      <div className="relative">
        {/* Vertical rail — runs the full height of the group */}
        {/* positioned at left: 11px to bisect the 22px node circle */}
        <div
          className="absolute top-0 bottom-0 left-[10px] w-px bg-zinc-800"
          aria-hidden="true"
        />

        <ol className="space-y-0">
          {rows.map((item, idx) => (
            <TimelineEvent
              key={item.approval_id}
              item={item}
              isLast={idx === rows.length - 1}
            />
          ))}
        </ol>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// TimelineEvent: single event node + card
// ---------------------------------------------------------------------------
function TimelineEvent({
  item,
  isLast,
}: {
  item: ActivityItem;
  isLast: boolean;
}) {
  const cfg = STATUS_CONFIG[item.approval_status] ?? STATUS_CONFIG_FALLBACK;
  const Icon = cfg.icon;

  const createdTs = toDate(item.created_at);
  const consumedTs = item.consumed_at ? toDate(item.consumed_at) : null;

  const hasDetails =
    item.action_details_excerpt &&
    item.action_details_excerpt !== "{}" &&
    item.action_details_excerpt.trim().length > 0;

  return (
    <li className={`relative flex gap-4 ${isLast ? "pb-0" : "pb-4"}`}>
      {/* Node column (fixed 22px wide) */}
      <div className="relative z-10 flex-shrink-0 w-[22px] flex flex-col items-center">
        {/*
          Status node: circle with icon.
          Non-color a11y cue: icon is meaningful (Clock/Check/X/Activity),
          plus aria-label on the li below.
        */}
        <div
          className={`
            w-[22px] h-[22px] rounded-full border flex items-center justify-center
            ${cfg.nodeBg} ${cfg.nodeRing}
          `}
          aria-hidden="true"
        >
          <Icon size={11} className={cfg.iconColor} strokeWidth={2} />
        </div>
      </div>

      {/* Event card */}
      <div
        className="flex-1 min-w-0 mb-1"
        role="listitem"
        aria-label={`${cfg.label} — ${item.action_type} (${item.requested_by})`}
      >
        <div
          className="
            border border-zinc-800/80 bg-zinc-950
            hover:border-zinc-700/80 hover:bg-zinc-900/60
            transition-colors duration-150
          "
        >
          {/* Card top row */}
          <div className="flex items-start justify-between gap-3 px-4 pt-3 pb-2">
            <div className="flex items-center gap-2 min-w-0 flex-wrap">
              {/* Status chip */}
              <span
                className={`
                  inline-flex items-center gap-1 text-[10px] font-mono px-1.5 py-0.5
                  border ${cfg.labelBg} ${cfg.labelText} ${cfg.labelBorder}
                  flex-shrink-0
                `}
                aria-label={`상태: ${cfg.label}`}
              >
                <Icon
                  size={9}
                  className={cfg.iconColor}
                  strokeWidth={2}
                  aria-hidden="true"
                />
                {cfg.label}
              </span>

              {/* Action title */}
              <span className="text-[13px] font-medium text-zinc-100 tracking-tight truncate">
                {actionLabel(item.action_type)}
              </span>

              {/* action_type identifier — monospace, for DBA scanning */}
              <span className="text-[10px] font-mono text-zinc-600 truncate hidden sm:inline">
                {item.action_type}
              </span>

              {/* Cluster */}
              {item.cluster_id && (
                <span className="text-[10px] font-mono text-zinc-500 truncate">
                  · {item.cluster_id}
                </span>
              )}
            </div>

            {/* Timestamp block — right-aligned, never wraps */}
            <div className="text-[11px] text-zinc-500 tabular-nums flex-shrink-0 text-right leading-relaxed">
              {createdTs ? fmtTime(createdTs) : "—"}
              {consumedTs && (
                <div className="text-[10px] text-sky-500/70 font-mono">
                  executed {fmtTime(consumedTs)}
                </div>
              )}
            </div>
          </div>

          {/* Actor line */}
          <div className="flex items-center gap-3 px-4 pb-2.5 font-mono text-[10px] text-zinc-600">
            <span>
              <span className="text-zinc-500">
                {item.requested_by || "agent"}
              </span>{" "}
              요청
            </span>
            {item.approved_by && (
              <>
                <span className="text-zinc-700">·</span>
                <span>
                  <span className="text-zinc-500">{item.approved_by}</span> 승인
                </span>
              </>
            )}
            <span className="text-zinc-700 ml-auto select-all">
              {item.approval_id.slice(0, 8)}
            </span>
          </div>

          {/* Details excerpt — only when present */}
          {hasDetails && (
            <pre
              className="
              text-[11px] text-zinc-400 font-mono whitespace-pre-wrap break-words
              max-h-20 overflow-y-auto
              border-t border-zinc-800/60 bg-zinc-900/60
              px-4 py-2 mx-0
            "
            >
              {item.action_details_excerpt}
            </pre>
          )}
        </div>
      </div>
    </li>
  );
}
