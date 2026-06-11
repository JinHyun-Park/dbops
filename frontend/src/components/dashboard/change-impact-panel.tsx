"use client";

import { useEffect, useState } from "react";
import {
  fetchChangeImpact,
  type ChangeImpactEvent,
  type ChangeImpactDelta,
} from "@/lib/api-client";
import { fmtDecimal } from "@/lib/format";

function relTime(iso: string) {
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  return `${Math.floor(h / 24)}일 전`;
}

// 변경 효과 판정: direction(lower/higher가 좋음)과 delta 방향을 결합.
// |변화율| 5% 미만은 노이즈로 보고 중립 처리 — 작은 출렁임을 개선/악화로
// 과대 해석하지 않는다.
function verdict(d: ChangeImpactDelta): "improve" | "regress" | "flat" {
  if (d.direction === "neutral") return "flat";
  if (d.delta_pct !== null && Math.abs(d.delta_pct) < 5) return "flat";
  const lowerIsBetter = d.direction === "lower";
  const wentDown = d.delta < 0;
  const improved = lowerIsBetter ? wentDown : !wentDown;
  return improved ? "improve" : "regress";
}

const VERDICT_STYLE: Record<string, string> = {
  improve: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  regress: "border-rose-500/40 bg-rose-500/10 text-rose-300",
  flat: "border-zinc-700 bg-zinc-800/40 text-zinc-400",
};

function DeltaChip({ d }: { d: ChangeImpactDelta }) {
  const v = verdict(d);
  const arrow = d.delta > 0 ? "▲" : d.delta < 0 ? "▼" : "·";
  const pct =
    d.delta_pct !== null
      ? `${d.delta_pct > 0 ? "+" : ""}${fmtDecimal(d.delta_pct, 1)}%`
      : "—";
  return (
    <div
      className={`px-2 py-1 border text-[11px] ${VERDICT_STYLE[v]}`}
      title={`${d.label}: ${fmtDecimal(d.before, 2)} → ${fmtDecimal(
        d.after,
        2,
      )} (변경 전후 평균)`}
    >
      <span className="font-medium">{d.label}</span>{" "}
      <span className="font-mono">
        {arrow} {pct}
      </span>
    </div>
  );
}

export function ChangeImpactPanel({ clusterId }: { clusterId: string }) {
  const [changes, setChanges] = useState<ChangeImpactEvent[]>([]);
  const [windowHours, setWindowHours] = useState(2);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    fetchChangeImpact(clusterId, windowHours, 7)
      .then((r) => {
        if (alive) setChanges(r.changes);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "조회 실패");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [clusterId, windowHours]);

  return (
    <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-4">
      <div className="flex items-center justify-between mb-1 gap-3">
        <div className="text-sm text-zinc-200 font-medium">변경 영향 회고</div>
        <div className="flex items-center gap-1 text-[10px]">
          <span className="text-zinc-500 mr-1">전후 윈도우</span>
          {[1, 2, 6].map((h) => (
            <button
              key={h}
              onClick={() => setWindowHours(h)}
              className={`px-1.5 py-0.5 border transition-colors ${
                windowHours === h
                  ? "border-amber-500 text-amber-300"
                  : "border-zinc-700 text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {h}h
            </button>
          ))}
        </div>
      </div>
      <div className="text-[11px] text-zinc-500 mb-3">
        최근 7일 RDS 변경 이벤트(파라미터·스케일링·재시작 등)를 앵커로 전후 ±
        {windowHours}시간 워크로드를 자동 비교합니다. 콘솔/CLI 직접 변경도
        포착합니다.
      </div>

      {error ? (
        <div className="text-xs text-rose-400">
          불러오지 못했습니다: {error}
        </div>
      ) : loading ? (
        <div className="text-zinc-500 text-sm py-2">불러오는 중…</div>
      ) : changes.length === 0 ? (
        <div className="text-zinc-500 text-sm py-2">
          최근 7일간 기록된 변경 이벤트가 없습니다.
        </div>
      ) : (
        <div className="space-y-3">
          {changes.map((c) => (
            <div
              key={c.event_id}
              className="border border-zinc-700/60 rounded p-3 bg-zinc-900/40"
            >
              <div className="flex items-baseline justify-between gap-3 mb-2">
                <div className="text-xs text-zinc-200">
                  <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                    {c.event_type}
                  </span>
                  {c.message || "(설명 없음)"}
                </div>
                <div
                  className="text-[10px] text-zinc-500 whitespace-nowrap"
                  title={c.event_time}
                >
                  {relTime(c.event_time)}
                </div>
              </div>
              {c.deltas.length === 0 ? (
                <div className="text-[11px] text-zinc-600">
                  전후 메트릭 표본이 부족해 비교를 생략했습니다(변경 직후이거나
                  수집 데이터 부족).
                </div>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {c.deltas.map((d) => (
                    <DeltaChip key={d.metric} d={d} />
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
