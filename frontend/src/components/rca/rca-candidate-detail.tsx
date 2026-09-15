"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

// Why this file exists: /tasks rendered a ranked candidate as "summary + score
// 3.42" and nothing else. diagnose_root_cause returns the derivation of that
// number and the measurements behind the sentence, and none of it reached the
// screen, so the ranking read as an opaque verdict. A DBA cannot act on
// "score 3.42"; they can act on "base 5.0 for a schema change, times 0.68
// because it happened 14 minutes before the anchor".
//
// Kept to the data the ranker actually emits. Nothing here recomputes a score
// or invents a label: a UI that derives its own number will disagree with the
// backend the first time a weight changes.

const CATEGORY_LABEL: Record<string, string> = {
  schema_change: "스키마 변경",
  event: "이벤트",
  blocking: "락 경합",
  counter_spike: "카운터 급증",
  elasticache_spike: "ElastiCache",
  metric_spike: "메트릭 급증",
  slow_query: "슬로우 쿼리",
};

// score_breakdown key -> Korean label. Keys the ranker adds later fall through
// to their raw name rather than being dropped, so a new factor is visible
// immediately instead of waiting for this table to be updated.
const FACTOR_LABEL: Record<string, string> = {
  base_weight: "기본 가중치",
  recency_factor: "최근성 계수",
  spike_factor: "급증 배율",
  severity_factor: "심각도 계수",
  magnitude_factor: "규모 배율",
  formula: "계산식",
  qualified_by: "판정 근거",
  lone_sample: "단일 샘플",
};

const EVIDENCE_LABEL: Record<string, string> = {
  metric_type: "메트릭",
  window_avg: "구간 평균",
  baseline_avg: "베이스라인 평균",
  ratio: "평균 비율",
  window_max: "구간 최댓값",
  peak_ratio: "최댓값 비율",
  window_samples: "구간 샘플 수",
  query_hash: "쿼리 해시",
  calls: "호출 수",
  total_time_ms: "총 실행시간(ms)",
  mean_time_ms: "평균 실행시간(ms)",
  schema_name: "스키마",
  snapshot_time: "스냅샷 시각",
  blocked_sessions: "대기 세션",
  max_wait_sec: "최대 대기(초)",
};

// `qualified_by` decides which FACT the score came from, and the two mean
// different incidents, so the UI spells them out rather than echoing the enum.
const QUALIFIED_BY_TEXT: Record<string, string> = {
  average: "구간 평균이 기준을 넘었습니다 (지속 상승)",
  peak: "구간 최댓값이 기준을 넘었습니다 (단기 포화)",
};

function fmtValue(v: unknown): string {
  if (v === null || v === undefined) return "-";
  if (typeof v === "boolean") return v ? "예" : "아니오";
  if (typeof v === "number") {
    return Number.isInteger(v) ? v.toLocaleString("ko-KR") : String(v);
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function categoryLabel(category: string | undefined): string {
  if (!category) return "기타";
  return CATEGORY_LABEL[category] ?? category;
}

interface Props {
  breakdown?: Record<string, unknown>;
  evidence?: Record<string, unknown>;
  suggestedAction?: string;
}

export function RcaCandidateDetail({
  breakdown,
  evidence,
  suggestedAction,
}: Props) {
  const [open, setOpen] = useState(false);
  const hasBreakdown = !!breakdown && Object.keys(breakdown).length > 0;
  const hasEvidence = !!evidence && Object.keys(evidence).length > 0;
  if (!hasBreakdown && !hasEvidence && !suggestedAction) return null;

  const qualifiedBy =
    typeof breakdown?.qualified_by === "string"
      ? (breakdown.qualified_by as string)
      : undefined;
  const loneSample = breakdown?.lone_sample === true;

  return (
    <div className="mt-1">
      {/* Collapsed by default: a report with eight candidates would otherwise
          open as a wall of tables, which is the same unreadable as showing
          nothing. The two facts worth seeing without a click are how the
          metric qualified and whether it stands on a single sample. */}
      <div className="flex flex-wrap items-center gap-1.5">
        <button
          onClick={() => setOpen((v) => !v)}
          className="inline-flex items-center gap-1 text-[11px] text-zinc-500 hover:text-zinc-300 transition-colors"
          aria-expanded={open}
        >
          {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          {open ? "근거 접기" : "점수 근거"}
        </button>
        {qualifiedBy && (
          <span
            className="text-[10px] font-mono px-1.5 py-0.5 border border-zinc-700 text-zinc-400"
            title={QUALIFIED_BY_TEXT[qualifiedBy] ?? qualifiedBy}
          >
            {qualifiedBy === "peak" ? "최댓값 판정" : "평균 판정"}
          </span>
        )}
        {loneSample && (
          <span
            className="text-[10px] font-mono px-1.5 py-0.5 border border-amber-500/40 text-amber-300/90"
            title="구간의 초과분을 최댓값 한 개가 전부 설명합니다. 정상적인 버스트나 잘못 기록된 값과 구분할 수 없어 점수를 낮춰 반영했습니다."
          >
            단일 샘플 (신뢰도 하향)
          </span>
        )}
      </div>

      {open && (
        <div className="mt-2 flex flex-col gap-2 border-l border-zinc-800 pl-3">
          {hasBreakdown && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                점수 계산
              </div>
              <div className="flex flex-col gap-0.5">
                {Object.entries(breakdown!).map(([k, v]) => (
                  <div
                    key={k}
                    className="flex items-baseline gap-2 text-[11px]"
                  >
                    <span className="text-zinc-500 w-24 flex-shrink-0">
                      {FACTOR_LABEL[k] ?? k}
                    </span>
                    <span className="font-mono text-zinc-300 break-all">
                      {k === "qualified_by"
                        ? QUALIFIED_BY_TEXT[String(v)] ?? fmtValue(v)
                        : fmtValue(v)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {hasEvidence && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                측정값
              </div>
              <div className="flex flex-col gap-0.5">
                {Object.entries(evidence!)
                  // query_text is rendered on its own below: inline it would
                  // blow out the two-column grid every other row uses.
                  .filter(([k]) => k !== "query_text")
                  .map(([k, v]) => (
                    <div
                      key={k}
                      className="flex items-baseline gap-2 text-[11px]"
                    >
                      <span className="text-zinc-500 w-24 flex-shrink-0">
                        {EVIDENCE_LABEL[k] ?? k}
                      </span>
                      <span className="font-mono text-zinc-300 break-all">
                        {fmtValue(v)}
                      </span>
                    </div>
                  ))}
              </div>
              {typeof evidence!.query_text === "string" && (
                <pre className="mt-1.5 text-[11px] font-mono text-zinc-400 bg-zinc-900 border border-zinc-800 p-2 overflow-x-auto whitespace-pre-wrap">
                  {evidence!.query_text as string}
                </pre>
              )}
            </div>
          )}

          {suggestedAction && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                이 신호에 대한 조치
              </div>
              <div className="text-[11px] text-zinc-300">{suggestedAction}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface ScoringPolicyProps {
  weights?: Record<string, number>;
  note?: string;
  schemaObservation?: Record<string, unknown>;
}

export function RcaScoringPolicy({
  weights,
  note,
  schemaObservation,
}: ScoringPolicyProps) {
  const [open, setOpen] = useState(false);
  const hasWeights = !!weights && Object.keys(weights).length > 0;
  const hasObs =
    !!schemaObservation && Object.keys(schemaObservation).length > 0;
  if (!hasWeights && !note && !hasObs) return null;

  return (
    <div className="mt-3 border-t border-zinc-800/60 pt-3">
      <button
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider text-zinc-500 hover:text-zinc-300 transition-colors"
        aria-expanded={open}
      >
        {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        채점 기준
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-2">
          {note && <div className="text-[11px] text-zinc-400">{note}</div>}
          {hasWeights && (
            <div className="flex flex-col gap-0.5">
              {Object.entries(weights!)
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => (
                  <div key={k} className="flex items-center gap-3 text-[11px]">
                    <span className="text-zinc-400 w-28 flex-shrink-0">
                      {categoryLabel(k)}
                    </span>
                    <span className="font-mono text-zinc-300 w-10">{v}</span>
                    {/* Proportional bar, because the ordering IS the policy:
                        a schema change at 5.0 outranking a metric spike at 2.0
                        is a stated choice, not an accident of the data. */}
                    <span
                      className="h-1 bg-zinc-600"
                      style={{ width: `${(v / 5) * 100}px` }}
                    />
                  </div>
                ))}
            </div>
          )}
          {hasObs && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                스키마 관측 범위
              </div>
              <div className="flex flex-col gap-0.5">
                {Object.entries(schemaObservation!).map(([k, v]) => (
                  <div
                    key={k}
                    className="flex items-baseline gap-2 text-[11px]"
                  >
                    <span className="text-zinc-500 w-28 flex-shrink-0">
                      {k}
                    </span>
                    <span className="font-mono text-zinc-300 break-all">
                      {fmtValue(v)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
