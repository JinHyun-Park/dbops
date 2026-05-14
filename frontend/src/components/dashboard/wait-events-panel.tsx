"use client";

import { useEffect, useState } from "react";
import { fetchWaitEvents } from "@/lib/api-client";

interface WaitEvent {
  wait_event: string;
  wait_type: string;
  avg_load: number | string;
  max_load: number | string;
}

const TYPE_COLORS: Record<string, string> = {
  CPU: "bg-emerald-500",
  IO: "bg-amber-500",
  Lock: "bg-rose-500",
  LWLock: "bg-rose-400",
  IPC: "bg-violet-500",
  Client: "bg-sky-500",
  Timeout: "bg-orange-500",
  Sync: "bg-rose-400", // MySQL: wait/synch/*
  Idle: "bg-zinc-600", // MySQL: wait/idle/* (foreground threads waiting for work)
  Other: "bg-zinc-500",
};

export function WaitEventsPanel({
  clusterId,
  hours = 1,
}: {
  clusterId: string;
  hours?: number;
}) {
  const [events, setEvents] = useState<WaitEvent[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchWaitEvents(clusterId, hours)
        .then((d) => {
          if (cancelled) return;
          setEvents(d.wait_events || []);
          setLoading(false);
        })
        .catch(() => !cancelled && setLoading(false));
    };
    load();
    const iv = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId, hours]);

  const total = events.reduce((s, e) => s + Number(e.avg_load || 0), 0);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">
        Wait Events ({hours}h)
      </div>
      {loading ? (
        <div className="text-xs text-zinc-500">Loading...</div>
      ) : events.length === 0 ? (
        <div className="text-xs text-zinc-500">no wait event data</div>
      ) : (
        <div className="space-y-2">
          {events.slice(0, 8).map((e) => {
            const avg = Number(e.avg_load || 0);
            const pct = total > 0 ? (avg / total) * 100 : 0;
            const colorClass = TYPE_COLORS[e.wait_type] || "bg-zinc-500";
            return (
              <div key={`${e.wait_event}-${e.wait_type}`}>
                <div className="flex justify-between text-xs mb-1">
                  <span
                    className="text-zinc-300 truncate max-w-[60%]"
                    title={e.wait_event}
                  >
                    <span className="text-zinc-500 mr-1">[{e.wait_type}]</span>
                    {e.wait_event}
                  </span>
                  <span className="text-zinc-400 font-mono">
                    {avg.toFixed(2)}{" "}
                    <span className="text-zinc-600">({pct.toFixed(1)}%)</span>
                  </span>
                </div>
                <div className="h-1.5 bg-zinc-900 rounded-full overflow-hidden">
                  <div
                    className={`h-full ${colorClass}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
