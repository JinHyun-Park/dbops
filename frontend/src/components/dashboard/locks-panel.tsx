"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchBlockingLocks } from "@/lib/api-client";

interface Lock {
  snapshot_time: string;
  blocked_pid: number | string;
  blocked_user: string;
  blocking_pid: number | string;
  blocking_user: string;
  blocked_query: string;
  blocking_query: string;
  locktype: string;
  blocked_mode: string;
  blocking_mode: string;
  relation: string;
  blocked_duration_sec: number | string;
}

function n(v: unknown) {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

function fmtDuration(s: number) {
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

type View = "list" | "chain";

// Build a directed graph blocker_pid → [{ blocked_pid, edge }]. We dedupe
// edges by `${holder}:${waiter}:${relation}` so a row showing the same PID
// pair on multiple objects doesn't create duplicate children.
interface Edge {
  holder: number;
  waiter: number;
  relation: string;
  blocked_mode: string;
  blocking_mode: string;
  blocked_duration_sec: number;
  blocked_user: string;
  blocking_user: string;
  blocked_query: string;
  blocking_query: string;
}

function buildChains(locks: Lock[]): {
  roots: number[];
  children: Record<number, Edge[]>;
  pidMeta: Record<number, { user?: string; query?: string }>;
} {
  const edges: Edge[] = locks.map((l) => ({
    holder: n(l.blocking_pid),
    waiter: n(l.blocked_pid),
    relation: l.relation || "",
    blocked_mode: l.blocked_mode || "",
    blocking_mode: l.blocking_mode || "",
    blocked_duration_sec: n(l.blocked_duration_sec),
    blocked_user: l.blocked_user || "",
    blocking_user: l.blocking_user || "",
    blocked_query: l.blocked_query || "",
    blocking_query: l.blocking_query || "",
  }));

  const children: Record<number, Edge[]> = {};
  const blockedSet = new Set<number>();
  const pidMeta: Record<number, { user?: string; query?: string }> = {};
  for (const e of edges) {
    if (!children[e.holder]) children[e.holder] = [];
    children[e.holder].push(e);
    blockedSet.add(e.waiter);
    if (!pidMeta[e.holder]) pidMeta[e.holder] = { user: e.blocking_user, query: e.blocking_query };
    if (!pidMeta[e.waiter]) pidMeta[e.waiter] = { user: e.blocked_user, query: e.blocked_query };
  }

  // Root holders = PIDs that are blocking somebody but never appear as a waiter.
  const roots = Object.keys(children)
    .map(Number)
    .filter((pid) => !blockedSet.has(pid))
    .sort((a, b) => a - b);
  return { roots, children, pidMeta };
}

function ChainNode({
  pid,
  edge,
  depth,
  children,
  pidMeta,
  visited,
}: {
  pid: number;
  edge?: Edge;
  depth: number;
  children: Record<number, Edge[]>;
  pidMeta: Record<number, { user?: string; query?: string }>;
  visited: Set<number>;
}) {
  const isRoot = depth === 0;
  const cycle = visited.has(pid);
  const nextVisited = new Set(visited);
  nextVisited.add(pid);
  const kids = children[pid] || [];
  const meta = pidMeta[pid] || {};
  const sev = edge ? (edge.blocked_duration_sec > 60 ? "rose" : edge.blocked_duration_sec > 10 ? "amber" : "zinc") : "amber";
  const dotColor = isRoot
    ? "bg-amber-400"
    : sev === "rose"
    ? "bg-rose-400"
    : sev === "amber"
    ? "bg-amber-300"
    : "bg-zinc-400";

  return (
    <div className="leading-snug">
      <div className="flex items-start gap-2 py-1 pr-2" style={{ paddingLeft: `${depth * 1.5 + 0.25}rem` }}>
        <span className={`w-2 h-2 rounded-full ${dotColor} mt-1.5 flex-shrink-0`} />
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2 flex-wrap">
            <span className={`font-mono text-xs ${isRoot ? "text-amber-300" : "text-rose-300"}`}>
              PID {pid}
            </span>
            <span className="text-[10px] text-zinc-500">{meta.user || "?"}</span>
            {isRoot && (
              <span className="text-[9px] px-1 py-0.5 border border-amber-500/40 text-amber-300 rounded-sm">
                root holder
              </span>
            )}
            {edge && (
              <>
                <span className="text-[10px] text-zinc-600">·</span>
                <span className="text-[10px] text-zinc-400">
                  waiting <span className="font-mono">{edge.blocked_mode}</span> on{" "}
                  <span className="font-mono">{edge.relation || "?"}</span>
                </span>
                <span className={`text-[10px] font-mono text-${sev}-400`}>
                  {fmtDuration(edge.blocked_duration_sec)}
                </span>
              </>
            )}
            {cycle && (
              <span className="text-[9px] px-1 py-0.5 border border-rose-500/40 text-rose-300 rounded-sm">
                cycle
              </span>
            )}
          </div>
          {meta.query && (
            <div
              className="text-[10px] font-mono text-zinc-500 truncate mt-0.5"
              title={meta.query}
            >
              {meta.query.slice(0, 120)}
            </div>
          )}
        </div>
      </div>
      {!cycle &&
        kids.map((k, i) => (
          <ChainNode
            key={`${k.waiter}-${i}`}
            pid={k.waiter}
            edge={k}
            depth={depth + 1}
            children={children}
            pidMeta={pidMeta}
            visited={nextVisited}
          />
        ))}
    </div>
  );
}

export function LocksPanel({ clusterId }: { clusterId: string }) {
  const [locks, setLocks] = useState<Lock[]>([]);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<View>("list");

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchBlockingLocks(clusterId)
        .then((d) => !cancelled && setLocks(d.locks || []))
        .catch(() => !cancelled && setLocks([]))
        .finally(() => !cancelled && setLoading(false));
    load();
    const iv = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId]);

  const graph = useMemo(() => buildChains(locks), [locks]);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-center justify-between">
        <div>
          <div className="text-xs text-zinc-400 uppercase tracking-wider">
            Blocking Locks
            {locks.length > 0 && (
              <span className="ml-2 px-1.5 py-0.5 bg-rose-500/20 text-rose-300 rounded text-[10px]">
                {locks.length}
              </span>
            )}
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            transactions blocked by other locks (last 15 minutes)
          </div>
        </div>
        {locks.length > 0 && (
          <div className="inline-flex items-center gap-0.5 border border-zinc-800 p-0.5">
            <button
              onClick={() => setView("list")}
              className={`text-[10px] uppercase tracking-wider px-2 py-1 transition-colors ${
                view === "list" ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:text-zinc-200"
              }`}
              title="Flat list — one row per (blocked, blocker) pair"
            >
              list
            </button>
            <button
              onClick={() => setView("chain")}
              className={`text-[10px] uppercase tracking-wider px-2 py-1 transition-colors ${
                view === "chain" ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:text-zinc-200"
              }`}
              title="Dependency chain — root holders → waiters, recursive"
            >
              chain
            </button>
          </div>
        )}
      </div>
      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">Loading...</div>
      ) : locks.length === 0 ? (
        <div className="p-6 text-emerald-400 text-sm flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-500" />
          no blocking locks detected
        </div>
      ) : view === "chain" ? (
        <div className="max-h-[28rem] overflow-y-auto py-2">
          {graph.roots.length === 0 ? (
            <div className="p-6 text-zinc-500 text-sm">
              cyclic deadlock detected — no clear root holder. See list view for raw edges.
            </div>
          ) : (
            graph.roots.map((rootPid) => (
              <div key={rootPid} className="border-l-2 border-amber-500/40 my-1">
                <ChainNode
                  pid={rootPid}
                  depth={0}
                  children={graph.children}
                  pidMeta={graph.pidMeta}
                  visited={new Set()}
                />
              </div>
            ))
          )}
          <div className="px-4 py-2 mt-1 text-[10px] text-zinc-600 border-t border-zinc-800">
            🟠 amber dot = root holder · 🔴 rose dot = blocked transaction (longer = worse) · indent = depth in the chain
          </div>
        </div>
      ) : (
        <div className="max-h-96 overflow-y-auto">
          <div className="divide-y divide-zinc-700">
            {locks.map((l, i) => {
              const dur = n(l.blocked_duration_sec);
              const sev = dur > 60 ? "rose" : dur > 10 ? "amber" : "zinc";
              const bg = sev === "rose" ? "bg-rose-950/20" : sev === "amber" ? "bg-amber-950/10" : "";
              return (
                <div key={`${l.blocked_pid}-${l.blocking_pid}-${i}`} className={`p-3 ${bg}`}>
                  <div className="flex items-baseline justify-between mb-2">
                    <div className="text-xs">
                      <span className="text-rose-400 font-mono">PID {n(l.blocked_pid)}</span>
                      <span className="text-zinc-500"> blocked by </span>
                      <span className="text-amber-400 font-mono">PID {n(l.blocking_pid)}</span>
                      <span className="text-zinc-500"> on </span>
                      <span className="text-zinc-300 font-mono">{l.relation}</span>
                    </div>
                    <div className={`text-xs font-mono text-${sev}-400`}>{fmtDuration(dur)}</div>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-[11px]">
                    <div>
                      <div className="text-zinc-500 mb-0.5">
                        BLOCKED ({l.blocked_user}) · {l.blocked_mode}
                      </div>
                      <pre className="bg-zinc-950 border border-zinc-800 rounded p-2 font-mono text-xs text-zinc-200 truncate whitespace-pre-wrap">
                        {l.blocked_query || "(unknown)"}
                      </pre>
                    </div>
                    <div>
                      <div className="text-zinc-500 mb-0.5">
                        HOLDING ({l.blocking_user}) · {l.blocking_mode}
                      </div>
                      <pre className="bg-zinc-950 border border-zinc-800 rounded p-2 font-mono text-xs text-zinc-200 truncate whitespace-pre-wrap">
                        {l.blocking_query || "(unknown)"}
                      </pre>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
