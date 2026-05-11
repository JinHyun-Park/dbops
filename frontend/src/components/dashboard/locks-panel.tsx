"use client";

import { useEffect, useState } from "react";
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

export function LocksPanel({ clusterId }: { clusterId: string }) {
  const [locks, setLocks] = useState<Lock[]>([]);
  const [loading, setLoading] = useState(true);

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
      </div>
      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">Loading...</div>
      ) : locks.length === 0 ? (
        <div className="p-6 text-emerald-400 text-sm flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-500" />
          no blocking locks detected
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
