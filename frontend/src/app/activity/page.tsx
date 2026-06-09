"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchActivity,
  fetchClusters,
  type ActivityItem,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  EmptyState,
} from "@/components/design-system/page-shell";
import { getSelectedCluster } from "@/lib/selected-cluster";

const STATUS_STYLE: Record<string, { label: string; chip: string }> = {
  pending: {
    label: "pending",
    chip: "bg-amber-500/10 text-amber-300 border-amber-500/40",
  },
  approved: {
    label: "approved",
    chip: "bg-emerald-500/10 text-emerald-300 border-emerald-500/40",
  },
  rejected: {
    label: "rejected",
    chip: "bg-rose-500/10 text-rose-300 border-rose-500/40",
  },
  consumed: {
    label: "executed",
    chip: "bg-zinc-700 text-zinc-200 border-zinc-600",
  },
};

const ACTION_OPTIONS = [
  "",
  "execute_sql",
  "modify_parameter",
  "modify_scaling",
  "manage_maintenance",
  "other",
];

interface ClusterRow {
  cluster_id: string;
}

export default function ActivityPage() {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [clusters, setClusters] = useState<ClusterRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Filters — seed the cluster filter from the global selection (⌘K / header /
  // other pages) so a focused DBA lands on "their" cluster's activity; falls
  // back to "" = all clusters when nothing is selected yet.
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

  // Group rows by date to make scanning a long retro log easier.
  const groups = useMemo(() => {
    const m = new Map<string, ActivityItem[]>();
    for (const it of items) {
      const day = (it.created_at || "").slice(0, 10) || "(no date)";
      if (!m.has(day)) m.set(day, []);
      m.get(day)!.push(it);
    }
    return Array.from(m.entries());
  }, [items]);

  return (
    <PageBody>
      <PageHeader
        eyebrow="incident"
        title="Activity log"
        description="DBOps에서 일어난 모든 쓰기 의사결정의 시간순 기록 — 누가 요청했고 누가 승인했고 언제 실행됐는지. 컴플라이언스 감사와 사후 회고 (post-incident retro) 용도."
        actions={
          <div className="flex items-center gap-2">
            <select
              value={clusterFilter}
              onChange={(e) => setClusterFilter(e.target.value)}
              className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-3 py-1.5 focus:outline-none focus:border-amber-500/60"
            >
              <option value="">모든 cluster</option>
              {clusters.map((c) => (
                <option key={c.cluster_id} value={c.cluster_id}>
                  {c.cluster_id}
                </option>
              ))}
            </select>
            <select
              value={actionFilter}
              onChange={(e) => setActionFilter(e.target.value)}
              className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-3 py-1.5 focus:outline-none focus:border-amber-500/60"
            >
              {ACTION_OPTIONS.map((a) => (
                <option key={a} value={a}>
                  {a || "all actions"}
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
        <div className="text-sm text-zinc-500">불러오는 중…</div>
      ) : items.length === 0 ? (
        <EmptyState
          eyebrow="activity"
          title="기록된 활동이 없습니다"
          description="아직 DBOps를 통해 실행된 쓰기 작업이 없거나, 현재 필터에 매칭되는 기록이 없습니다."
          secondary={{ href: "/approvals", label: "대기 중인 승인 보기" }}
        />
      ) : (
        <div className="space-y-6">
          {groups.map(([day, rows]) => (
            <section key={day}>
              <div className="text-[11px] uppercase tracking-wider text-zinc-500 mb-2">
                {day}
              </div>
              <div className="border border-zinc-800 divide-y divide-zinc-800">
                {rows.map((it) => (
                  <ActivityRow key={it.approval_id} item={it} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </PageBody>
  );
}

function ActivityRow({ item }: { item: ActivityItem }) {
  const style = STATUS_STYLE[item.approval_status] || {
    label: item.approval_status,
    chip: "bg-zinc-700/40 text-zinc-300 border-zinc-700",
  };
  const ts = new Date(item.created_at);
  const consumedTs = item.consumed_at ? new Date(item.consumed_at) : null;
  return (
    <div className="px-4 py-3">
      <div className="flex items-baseline justify-between gap-3 mb-1">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={`text-[10px] font-mono px-1.5 py-0.5 border ${style.chip}`}
          >
            {style.label}
          </span>
          <span className="text-sm text-zinc-100 font-mono">
            {item.action_type}
          </span>
          <span className="text-[11px] text-zinc-500 font-mono truncate">
            · {item.cluster_id}
          </span>
        </div>
        <div className="text-[11px] text-zinc-500 tabular-nums flex-shrink-0">
          {ts.toLocaleTimeString()}{" "}
          {consumedTs && <>· executed {consumedTs.toLocaleTimeString()}</>}
        </div>
      </div>
      {item.action_details_excerpt && item.action_details_excerpt !== "{}" && (
        <pre className="text-[11px] text-zinc-400 font-mono whitespace-pre-wrap break-words max-h-24 overflow-y-auto bg-zinc-950 border border-zinc-800/60 p-2 mt-1">
          {item.action_details_excerpt}
        </pre>
      )}
      <div className="text-[10px] text-zinc-600 mt-1 font-mono flex items-center gap-3">
        <span>requested {item.requested_by || "agent"}</span>
        {item.approved_by && <span>· approved {item.approved_by}</span>}
        <span className="opacity-50">· {item.approval_id.slice(0, 8)}</span>
      </div>
    </div>
  );
}
