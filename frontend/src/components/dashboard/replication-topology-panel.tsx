"use client";

import { useEffect, useState } from "react";
import {
  fetchTopology,
  type TopologyMember,
  type TopologyResponse,
} from "@/lib/api-client";
import { fmtDecimal } from "@/lib/format";

// Auto-load on mount — topology is one fast describe call, low cost,
// and DBAs expect this panel to be populated when they land. Different
// from redundant-indexes (which scans every pg_index — expensive).

const LAG_THRESHOLDS = { warn: 100, crit: 1000 } as const;

function lagTone(ms: number | null): {
  text: string;
  classes: string;
  dot: string;
} {
  if (ms === null) {
    return {
      text: "no data",
      classes: "text-zinc-500 border-zinc-700 bg-zinc-900/40",
      dot: "bg-zinc-600",
    };
  }
  if (ms >= LAG_THRESHOLDS.crit) {
    return {
      text: `${fmtDecimal(ms, 0)} ms`,
      classes: "text-rose-300 border-rose-500/40 bg-rose-500/10",
      dot: "bg-rose-500",
    };
  }
  if (ms >= LAG_THRESHOLDS.warn) {
    return {
      text: `${fmtDecimal(ms, 0)} ms`,
      classes: "text-amber-300 border-amber-500/40 bg-amber-500/10",
      dot: "bg-amber-500",
    };
  }
  return {
    text: `${fmtDecimal(ms, ms < 10 ? 1 : 0)} ms`,
    classes: "text-emerald-300 border-emerald-500/40 bg-emerald-500/10",
    dot: "bg-emerald-500",
  };
}

function statusTone(status: string): string {
  const s = status.toLowerCase();
  if (s === "available") return "text-emerald-400";
  if (s.includes("modifying") || s.includes("backing-up"))
    return "text-amber-400";
  if (s.includes("failed") || s.includes("storage-full"))
    return "text-rose-400";
  return "text-zinc-400";
}

export function ReplicationTopologyPanel({ clusterId }: { clusterId: string }) {
  const [data, setData] = useState<TopologyResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await fetchTopology(clusterId);
      setData(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setData(null);
    setErr(null);
    if (clusterId) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterId]);

  const writer = data?.members.find((m) => m.is_writer);
  const readers = data?.members.filter((m) => !m.is_writer) ?? [];
  const maxLag = readers
    .map((r) => r.replica_lag_ms)
    .filter((v): v is number => v !== null);
  const maxLagMs = maxLag.length ? Math.max(...maxLag) : null;

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <div className="text-sm text-zinc-200 font-medium">
            Replication Topology
            {data?.members_count != null && (
              <span className="ml-2 px-1.5 py-0.5 bg-zinc-800 text-zinc-300 border border-zinc-700 text-[10px]">
                {data.members_count}{" "}
                {data.members_count === 1 ? "node" : "nodes"}
              </span>
            )}
            {data?.multi_az && (
              <span className="ml-1.5 px-1.5 py-0.5 bg-sky-500/10 text-sky-300 border border-sky-500/40 text-[10px]">
                Multi-AZ
              </span>
            )}
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            Writer + readers · 인스턴스별 Replica Lag (CloudWatch 15분 윈도우
            최신 datapoint)
          </div>
        </div>
        <div className="flex items-center gap-3">
          {maxLagMs !== null && readers.length > 0 && (
            <span className="text-[10px] text-zinc-500 font-mono">
              max lag{" "}
              <span
                className={
                  maxLagMs >= LAG_THRESHOLDS.crit
                    ? "text-rose-300"
                    : maxLagMs >= LAG_THRESHOLDS.warn
                      ? "text-amber-300"
                      : "text-emerald-300"
                }
              >
                {fmtDecimal(maxLagMs, 0)} ms
              </span>
            </span>
          )}
          <button
            onClick={load}
            disabled={loading}
            className="text-xs font-medium px-3 py-1 bg-zinc-800 text-zinc-200 hover:bg-zinc-700 disabled:opacity-50 transition-colors border border-zinc-700"
          >
            {loading ? "불러오는 중…" : "새로고침"}
          </button>
        </div>
      </div>

      <div>
        {!data && loading && (
          <div className="p-6 text-zinc-500 text-sm">불러오는 중…</div>
        )}
        {err && (
          <div className="p-5">
            <div className="text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
              {err}
            </div>
          </div>
        )}
        {data?.error && (
          <div className="p-5">
            <div
              className={`text-xs border px-3 py-2 ${
                data.info
                  ? "text-zinc-400 border-zinc-700 bg-zinc-800/30"
                  : "text-rose-300 border-rose-500/40 bg-rose-500/10"
              }`}
            >
              {data.error}
            </div>
          </div>
        )}
        {data && !data.error && !writer && readers.length === 0 && (
          <div className="p-6 text-zinc-500 text-sm">
            클러스터 멤버 정보가 비어 있습니다.
          </div>
        )}
        {data && !data.error && (writer || readers.length > 0) && (
          <div className="p-5 grid grid-cols-1 md:grid-cols-[minmax(220px,260px)_24px_1fr] gap-4 items-start">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5 font-medium">
                Writer
              </div>
              {writer ? (
                <NodeCard node={writer} isWriter />
              ) : (
                <div className="text-xs text-zinc-500 border border-zinc-800 bg-zinc-900/40 px-3 py-4">
                  writer 없음
                </div>
              )}
            </div>
            <div className="hidden md:flex flex-col items-center justify-center text-zinc-600 pt-8 font-mono text-xs">
              ──▶
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5 font-medium flex items-baseline gap-2">
                <span>Readers</span>
                <span className="text-zinc-600">({readers.length})</span>
              </div>
              {readers.length === 0 ? (
                <div className="text-xs text-zinc-500 border border-zinc-800 bg-zinc-900/40 px-3 py-4">
                  reader 없음 — single-node 클러스터입니다. 운영 환경이면 최소
                  1개 reader 추가를 권장 (failover RTO 단축).
                </div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                  {readers.map((r) => (
                    <NodeCard key={r.instance_id} node={r} />
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
        {data?.endpoint && (
          <div className="px-4 pb-3 pt-1 border-t border-zinc-800 text-[10px] font-mono text-zinc-500 flex flex-wrap gap-x-4 gap-y-1">
            <span>
              <span className="text-zinc-600">writer:</span> {data.endpoint}
            </span>
            {data.reader_endpoint && (
              <span>
                <span className="text-zinc-600">reader:</span>{" "}
                {data.reader_endpoint}
              </span>
            )}
            {data.engine && (
              <span>
                <span className="text-zinc-600">engine:</span> {data.engine}{" "}
                {data.engine_version}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function NodeCard({
  node,
  isWriter = false,
}: {
  node: TopologyMember;
  isWriter?: boolean;
}) {
  const lag = lagTone(node.replica_lag_ms);
  return (
    <div
      className={`border px-3 py-2 ${
        isWriter
          ? "border-sky-500/40 bg-sky-500/5"
          : "border-zinc-800 bg-zinc-900/60"
      }`}
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <span
          className={`text-xs font-mono break-all leading-tight ${
            isWriter ? "text-sky-200" : "text-zinc-200"
          }`}
        >
          {node.instance_id}
        </span>
        {isWriter ? (
          <span className="px-1.5 py-0.5 bg-sky-500/15 text-sky-300 border border-sky-500/40 text-[10px] uppercase tracking-wider shrink-0">
            writer
          </span>
        ) : (
          <span
            className={`px-1.5 py-0.5 border text-[10px] font-mono shrink-0 flex items-center gap-1 ${lag.classes}`}
            title="AuroraReplicaLag — CloudWatch 15분 윈도우 최신값"
          >
            <span className={`w-1.5 h-1.5 rounded-full ${lag.dot}`} />
            {lag.text}
          </span>
        )}
      </div>
      <div className="text-[10px] font-mono text-zinc-500 flex flex-wrap gap-x-3 gap-y-0.5">
        {node.instance_class && (
          <span>
            <span className="text-zinc-600">class:</span>{" "}
            <span className="text-zinc-400">{node.instance_class}</span>
          </span>
        )}
        {node.availability_zone && (
          <span>
            <span className="text-zinc-600">az:</span>{" "}
            <span className="text-zinc-400">{node.availability_zone}</span>
          </span>
        )}
        {node.instance_status && (
          <span>
            <span className="text-zinc-600">status:</span>{" "}
            <span className={statusTone(node.instance_status)}>
              {node.instance_status}
            </span>
          </span>
        )}
        {node.promotion_tier !== null &&
          node.promotion_tier !== undefined &&
          !isWriter && (
            <span title="Aurora promotion tier — lower = higher failover priority">
              <span className="text-zinc-600">tier:</span>{" "}
              <span className="text-zinc-400">{node.promotion_tier}</span>
            </span>
          )}
        {node.parameter_group_status &&
          node.parameter_group_status !== "in-sync" && (
            <span className="text-amber-300">
              pg-status: {node.parameter_group_status}
            </span>
          )}
      </div>
    </div>
  );
}
