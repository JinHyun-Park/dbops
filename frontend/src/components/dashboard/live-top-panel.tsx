"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, X, Pause, Play, Database } from "lucide-react";
import { fetchLiveActivity, type LiveActivity } from "@/lib/api-client";
import { fmtDecimal } from "@/lib/format";

// On-demand LIVE top (P2-⑧). A `top`/pg_activity-style view of the TARGET
// cluster. LOAD-SAFETY INVARIANT: the browser polls the live endpoint ~2s ONLY
// while this drawer is open, and the polling interval is cleared on close AND
// on unmount (the useEffect cleanup below). So the target DB sees load only
// while a DBA is actively watching — never as always-on background collection.

const POLL_MS = 2000;
const RATE_KEYS: [string, string][] = [
  ["xact_commit", "commits"],
  ["tup_inserted", "inserts"],
  ["tup_updated", "updates"],
  ["tup_deleted", "deletes"],
];

function ageLabel(s: number | null): string {
  if (s == null) return "—";
  if (s < 1) return "<1s";
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

// active / idle-in-transaction / idle are visually distinct — a long
// idle-in-transaction is the classic silent lock-holder, so it gets a warning color.
function stateClasses(state: string | null): string {
  const s = (state || "").toLowerCase();
  if (s === "active") return "text-emerald-300 border-emerald-500/40";
  if (s.startsWith("idle in transaction"))
    return "text-amber-300 border-amber-500/40";
  return "text-zinc-500 border-zinc-700";
}

export function LiveTopPanel({ clusterId }: { clusterId: string }) {
  const [open, setOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [data, setData] = useState<LiveActivity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rates, setRates] = useState<Record<string, number>>({});
  const [buffers, setBuffers] = useState<LiveActivity["buffercache"]>(null);
  const [buffersLoading, setBuffersLoading] = useState(false);
  // Previous cumulative counters + capture time — for client-side per-second
  // rate deltas. Ref (not state) so it doesn't retrigger the poll effect.
  const prevRef = useRef<{
    at: number;
    counters: Record<string, number>;
  } | null>(null);

  // Pause polling when the browser tab is hidden — no point hammering the
  // target for a view nobody is looking at.
  useEffect(() => {
    const onVis = () => setHidden(document.visibilityState === "hidden");
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  // THE poll loop. Guarded on open && !paused && !hidden. The cleanup clears
  // the interval whenever ANY dep changes (close/pause/hide/cluster switch) OR
  // the component unmounts — this is the load-safety guarantee.
  useEffect(() => {
    if (!open || paused || hidden) return;
    let cancelled = false;
    let id: ReturnType<typeof setInterval> | null = null;
    const stop = () => {
      if (id !== null) {
        clearInterval(id);
        id = null;
      }
    };
    // Reset the rate baseline on (re)start so a resume after a pause gap
    // doesn't compute a huge spurious spike from the stale snapshot.
    prevRef.current = null;
    const tick = async () => {
      try {
        const snap = await fetchLiveActivity(clusterId);
        if (cancelled) return;
        setData(snap);
        setError(null);
        // STOP polling once a snapshot reports the view can't be served
        // (non-PG, registry/Data-API unavailable). Otherwise we'd re-hit the
        // target/Lambda every POLL_MS while the user only sees the unavailable
        // message — the load-safety promise is "poll ONLY when actually live".
        // Reopening the drawer re-runs this effect and retries.
        if (!snap.available) {
          stop();
          return;
        }
        if (snap.db_counters && snap.captured_at) {
          const prev = prevRef.current;
          const cur = { at: snap.captured_at, counters: snap.db_counters };
          if (prev) {
            const dt = (cur.at - prev.at) / 1000;
            if (dt > 0) {
              const next: Record<string, number> = {};
              for (const [col, label] of RATE_KEYS) {
                const d =
                  Number(cur.counters[col]) - Number(prev.counters[col]);
                next[label] = d >= 0 ? d / dt : 0;
              }
              setRates(next);
            }
          }
          prevRef.current = cur;
        }
      } catch (e) {
        if (!cancelled)
          setError(e instanceof Error ? e.message : "라이브 조회 실패");
      }
    };
    tick(); // immediate first poll, then every POLL_MS
    id = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      stop();
    };
  }, [open, paused, hidden, clusterId]);

  const close = useCallback(() => {
    setOpen(false);
    setData(null);
    setError(null);
    setRates({});
    setBuffers(null);
    prevRef.current = null;
  }, []);

  // Esc closes.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, close]);

  const loadBuffers = useCallback(async () => {
    setBuffersLoading(true);
    try {
      const snap = await fetchLiveActivity(clusterId, { buffers: true });
      setBuffers(snap.buffercache ?? null);
    } catch {
      setBuffers({ available: false, reason: "버퍼풀 조회에 실패했습니다" });
    } finally {
      setBuffersLoading(false);
    }
  }, [clusterId]);

  const unavailable = data && !data.available;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 border border-zinc-700 text-zinc-300 hover:border-emerald-500/50 hover:text-emerald-200 transition-colors"
      >
        <Activity size={13} />
        라이브 세션 (top)
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex justify-end">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={close}
          />
          <div className="relative w-full max-w-3xl h-full bg-zinc-950 border-l border-zinc-800 shadow-2xl flex flex-col">
            {/* Header */}
            <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-zinc-800">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-sm font-medium text-zinc-100">
                  <Activity size={15} className="text-emerald-300" />
                  라이브 세션 (top)
                  {!paused && !hidden && !unavailable && (
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                  )}
                </div>
                <div className="text-[11px] font-mono text-zinc-500 mt-0.5 truncate">
                  {clusterId}
                </div>
              </div>
              <div className="flex items-center gap-2">
                {!unavailable && (
                  <button
                    onClick={() => setPaused((p) => !p)}
                    className="inline-flex items-center gap-1 text-[11px] px-2 py-1 border border-zinc-700 text-zinc-400 hover:text-zinc-200 hover:border-zinc-500 transition-colors"
                    title={paused ? "재개" : "일시정지"}
                  >
                    {paused ? <Play size={12} /> : <Pause size={12} />}
                    {paused ? "재개" : "일시정지"}
                  </button>
                )}
                <button
                  onClick={close}
                  className="text-zinc-500 hover:text-zinc-200 transition-colors"
                  title="닫기 (Esc)"
                >
                  <X size={18} />
                </button>
              </div>
            </div>

            {/* Load-safety notice */}
            <div className="px-5 py-2 border-b border-zinc-800/60 text-[11px] text-zinc-500">
              라이브 (이 창이 열려 있는 동안에만 대상 DB를 폴링합니다 · ~2초)
              {hidden && (
                <span className="text-amber-400/80">
                  {" "}
                  · 탭 비활성 — 일시중단됨
                </span>
              )}
              {paused && (
                <span className="text-amber-400/80"> · 일시정지됨</span>
              )}
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">
              {error && (
                <div className="px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
                  {error}
                </div>
              )}

              {unavailable ? (
                <div className="px-3 py-4 border border-zinc-700/60 bg-zinc-900/60 text-sm text-zinc-400">
                  {data?.reason || "라이브 조회를 사용할 수 없습니다."}
                </div>
              ) : (
                <>
                  {/* Per-second rates strip */}
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                    {RATE_KEYS.map(([, label]) => (
                      <div
                        key={label}
                        className="border border-zinc-800 bg-zinc-900/40 px-3 py-2"
                      >
                        <div className="text-[10px] uppercase tracking-wide text-zinc-500">
                          {label}/s
                        </div>
                        <div className="text-lg font-mono text-zinc-100 tabular-nums">
                          {label in rates ? fmtDecimal(rates[label], 1) : "—"}
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* Blocking chains */}
                  {data?.blocking && data.blocking.length > 0 && (
                    <div className="border border-rose-500/40 bg-rose-500/5">
                      <div className="px-3 py-2 text-xs font-medium text-rose-300 border-b border-rose-500/20">
                        블로킹 감지 ({data.blocking.length})
                      </div>
                      <div className="px-3 py-2 space-y-1 text-xs font-mono text-zinc-300">
                        {data.blocking.map((b) => (
                          <div key={b.pid}>
                            <span className="text-rose-300">
                              {b.blockers.join(", ")}
                            </span>
                            <span className="text-zinc-600"> → </span>
                            <span className="text-amber-200">{b.pid}</span>
                            <span className="text-zinc-600"> (blocked)</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Sessions table */}
                  <div>
                    <div className="text-xs text-zinc-500 mb-1.5">
                      활성 세션{" "}
                      {data?.sessions ? `(${data.sessions.length})` : ""} · age
                      내림차순
                    </div>
                    <div className="overflow-x-auto border border-zinc-800">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-[10px] uppercase tracking-wide text-zinc-500 border-b border-zinc-800">
                            <th className="text-left px-2 py-1.5 font-medium">
                              pid
                            </th>
                            <th className="text-left px-2 py-1.5 font-medium">
                              user
                            </th>
                            <th className="text-left px-2 py-1.5 font-medium">
                              state
                            </th>
                            <th className="text-left px-2 py-1.5 font-medium">
                              wait
                            </th>
                            <th className="text-right px-2 py-1.5 font-medium">
                              age
                            </th>
                            <th className="text-left px-2 py-1.5 font-medium">
                              query
                            </th>
                          </tr>
                        </thead>
                        <tbody className="font-mono">
                          {(data?.sessions || []).map((s) => (
                            <tr
                              key={s.pid}
                              className="border-b border-zinc-800/50 last:border-0"
                            >
                              <td className="px-2 py-1.5 text-zinc-300 tabular-nums">
                                {s.pid}
                              </td>
                              <td className="px-2 py-1.5 text-zinc-400">
                                {s.usename || "—"}
                              </td>
                              <td className="px-2 py-1.5">
                                <span
                                  className={`inline-block px-1.5 py-0.5 border text-[10px] ${stateClasses(
                                    s.state,
                                  )}`}
                                >
                                  {s.state || "—"}
                                </span>
                              </td>
                              <td className="px-2 py-1.5 text-zinc-400">
                                {s.wait || "CPU"}
                              </td>
                              <td className="px-2 py-1.5 text-right text-zinc-300 tabular-nums">
                                {ageLabel(s.age_sec)}
                              </td>
                              <td
                                className="px-2 py-1.5 text-zinc-400 max-w-md truncate"
                                title={s.query || ""}
                              >
                                {s.query || "—"}
                              </td>
                            </tr>
                          ))}
                          {data?.sessions && data.sessions.length === 0 && (
                            <tr>
                              <td
                                colSpan={6}
                                className="px-2 py-4 text-center text-zinc-600"
                              >
                                활성 세션이 없습니다
                              </td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {/* Buffer pool — HEAVY, manual one-off fetch only (never polled) */}
                  <div className="border border-zinc-800">
                    <div className="flex items-center justify-between px-3 py-2 border-b border-zinc-800">
                      <div className="text-xs text-zinc-400 flex items-center gap-1.5">
                        <Database size={13} className="text-sky-300" />
                        버퍼풀 (pg_buffercache)
                      </div>
                      <button
                        onClick={loadBuffers}
                        disabled={buffersLoading}
                        className="text-[11px] px-2 py-1 border border-zinc-700 text-zinc-400 hover:border-sky-500/50 hover:text-sky-200 disabled:opacity-50 transition-colors"
                      >
                        {buffersLoading ? "조회 중…" : "새로고침"}
                      </button>
                    </div>
                    <div className="px-3 py-2 text-xs text-zinc-400">
                      {!buffers ? (
                        <span className="text-zinc-600">
                          무거운 조회입니다 — 폴링에 포함되지 않으며 버튼을 눌러
                          1회만 조회합니다.
                        </span>
                      ) : buffers.available === false ? (
                        <span className="text-zinc-500">{buffers.reason}</span>
                      ) : (
                        <div className="space-y-1.5">
                          <div className="font-mono">
                            사용 {fmtDecimal(buffers.used ?? 0, 0)} /{" "}
                            {fmtDecimal(buffers.total ?? 0, 0)} 버퍼
                            {buffers.total
                              ? ` (${(
                                  ((buffers.used ?? 0) / buffers.total) *
                                  100
                                ).toFixed(1)}%)`
                              : ""}
                          </div>
                          {buffers.top_relations &&
                            buffers.top_relations.length > 0 && (
                              <div className="space-y-0.5 font-mono text-zinc-500">
                                {buffers.top_relations.map((r) => (
                                  <div
                                    key={r.relation}
                                    className="flex justify-between gap-4"
                                  >
                                    <span className="truncate">
                                      {r.relation}
                                    </span>
                                    <span className="tabular-nums">
                                      {fmtDecimal(r.buffers, 0)}
                                    </span>
                                  </div>
                                ))}
                              </div>
                            )}
                        </div>
                      )}
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
