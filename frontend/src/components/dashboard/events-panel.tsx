"use client";

interface Event {
  ts: string;
  event_type: string;
  severity: string;
  message: string;
}

const SEVERITY_STYLES: Record<string, string> = {
  critical: "border-l-red-500 bg-red-950/30",
  error: "border-l-red-400 bg-red-950/20",
  warning: "border-l-amber-500 bg-amber-950/20",
  info: "border-l-sky-500 bg-sky-950/10",
};

function relTime(iso: string) {
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function EventsPanel({ events }: { events: Event[] }) {
  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">Recent Events</div>
      {events.length === 0 ? (
        <div className="text-xs text-zinc-500 py-2">no recent events</div>
      ) : (
        <div className="space-y-2 max-h-96 overflow-y-auto">
          {events.map((e, i) => {
            const style = SEVERITY_STYLES[e.severity?.toLowerCase()] || "border-l-zinc-500 bg-zinc-900/30";
            return (
              <div key={`${e.ts}-${i}`} className={`border-l-2 ${style} pl-3 py-2 pr-2 rounded-r`}>
                <div className="flex justify-between items-baseline mb-0.5">
                  <span className="text-xs font-medium text-zinc-300">{e.event_type}</span>
                  <span className="text-[10px] text-zinc-500">{relTime(e.ts)}</span>
                </div>
                <div className="text-xs text-zinc-400 leading-snug">{e.message}</div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
