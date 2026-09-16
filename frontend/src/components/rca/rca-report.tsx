"use client";

/**
 * The RCA report, as a READING ORDER.
 *
 * What it replaces: seven sibling sections (narrative, recommendations,
 * candidates, note, signals_examined, scoring policy, trace) all rendered at
 * 10-11px with uppercase tracking labels. The conclusion and the audit trail
 * carried the same visual weight, and the candidate list, being the longest,
 * dominated. An operator asking "what happened, what do I do" had to read the
 * whole thing to find out.
 *
 * The order here, top to bottom:
 *   1. FIRST TWO SECONDS: which cluster, what fired it, the one-sentence
 *      leading hypothesis, when the report arrived, when the incident was, and
 *      a visible qualification when the evidence behind it is incomplete.
 *   2. THREE SHORT BLOCKS: the assessment (the Korean narrative), the
 *      supporting evidence (measurements with their own timestamps, taken from
 *      the evidence payload rather than from prose), and the next steps, split
 *      into read-only checks and operational changes.
 *   3. ONE CLICK AWAY: the alternative hypotheses and the analysis details
 *      (score derivation, weight table, source counts, trace, duration).
 *
 * THE GOVERNING CONSTRAINT: never make the report look more certain than the
 * system is. A recent schema change starts at base weight 5.0 and a slow query
 * at 2.0, which is an investigation policy, not proof of causation. So the
 * headline is written as a hypothesis, and a ranking score is never rendered as
 * a confidence percentage anywhere on this surface.
 *
 * The data shaping lives in @/lib/rca-report-model (pure, and checked by
 * tools/rca-report-check.mjs); this file only renders it.
 */

import { useEffect, useState } from "react";
import { ChevronRight } from "lucide-react";
import { fetchTask, type AgentTask } from "@/lib/api-client";
import {
  assessment,
  coverageGaps,
  nextSteps,
  observations,
  reportHeadline,
  topCaveats,
  type RcaCandidate,
} from "@/lib/rca-report-model";
import {
  RcaCandidateDetail,
  RcaScoringPolicy,
  categoryLabel,
  evidenceLabel,
  fmtEvidenceValue,
} from "@/components/rca/rca-candidate-detail";
import { fmtAgoKo, fmtClockKo, fmtExact } from "@/lib/format";
import { useT } from "@/lib/i18n";

export function RcaReport({ row }: { row: AgentTask }) {
  const t = useT();
  const [task, setTask] = useState<AgentTask | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // The list row is a summary by design, so the full report is its own read.
  // The header below renders from the row immediately and the body fills in.
  useEffect(() => {
    let alive = true;
    // No state reset first: every row owns its own instance of this component
    // and only mounts it while open, so `row.task_id` never changes under it.
    fetchTask(row.task_id)
      .then((d) => {
        if (alive) setTask(d);
      })
      .catch((e) => {
        if (alive) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [row.task_id]);

  const result = task?.result;
  const candidates = (result?.candidates ?? []) as RcaCandidate[];
  const top = candidates[0];
  const reportLines = result?.lines as
    | { label: string; value: string }[]
    | undefined;

  // The leading SUPPORTED candidate, and what kind of claim it is at all. The
  // list row carries the same sentence, so the two surfaces cannot disagree
  // about what the report says.
  const head = reportHeadline(row, top);
  // Only a ranked leader is a headline hypothesis. Kept as a plain string for
  // the narrative dedupe below, which compares against the sentence shown.
  const headline = head.kind === "hypothesis" ? head.text : "";
  const category =
    (top?.category as string | undefined) ?? row.finding?.category;
  const gaps = result ? coverageGaps(result) : [];
  const caveats = topCaveats(top);
  const steps = result ? nextSteps(result) : [];
  const checks = steps.filter((s) => s.kind === "check");
  const changes = steps.filter((s) => s.kind === "change");
  const narrativeParts = result?.narrative
    ? assessment(String(result.narrative), headline)
    : [];
  const obs = observations(candidates);
  const windowMinutes = result?.window_minutes
    ? Number(result.window_minutes)
    : null;
  const anchor = row.anchor_time || result?.anchor_time || null;

  return (
    <div className="border-t border-zinc-800/60 px-4 py-4 sm:px-5">
      {/* ── First two seconds ─────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-500">
        <span className="font-mono text-zinc-300">{row.cluster_id}</span>
        <span>{row.trigger}</span>
      </div>

      {/* A digest is titled as the report it is: no 유력 가설, and no caveat
          about a ranking, because no ranking ran. Its `lines` table below is
          the content. */}
      {head.kind === "digest" ? (
        <div className="mt-2 max-w-[68ch]">
          <div className="text-[11px] text-zinc-500">{t("예약 리포트")}</div>
          <p className="mt-1 text-[15px] leading-relaxed text-zinc-100">
            {head.text}
          </p>
          <p className="mt-1 text-xs text-zinc-500">
            {t("정기 점검 결과이며 원인 분석이 아닙니다")}
          </p>
        </div>
      ) : head.kind === "hypothesis" ? (
        <div className="mt-2 max-w-[68ch]">
          <div className="text-[11px] text-zinc-500">
            {t("유력 가설")}
            {category ? ` (${t(categoryLabel(category))})` : ""}
          </div>
          <p className="mt-1 text-[16px] leading-relaxed text-zinc-100">
            {headline}
          </p>
          <p className="mt-1 text-xs text-zinc-500">
            {t("순위는 조사 우선순위이며 확정된 원인이 아닙니다")}
            {row.finding && row.finding.candidate_count > 1
              ? `, ${t("후보 {n}건 중 1순위").replace(
                  "{n}",
                  String(row.finding.candidate_count),
                )}`
              : ""}
          </p>
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-zinc-500">
        {row.published_at && (
          <span>
            {t("리포트 도착")} {fmtAgoKo(row.published_at)} (
            {fmtClockKo(row.published_at)})
          </span>
        )}
        {/* A different clock, kept visibly separate: a report that arrived
            minutes ago can be about yesterday's incident. */}
        {anchor && (
          <span>
            {t("인시던트 발생")} {fmtClockKo(anchor)}
            {windowMinutes ? `, 분석 구간 ${windowMinutes}분` : ""}
          </span>
        )}
      </div>

      {/* The qualification sits WITH the claim it qualifies, which is why the
          standalone "note" section is gone. */}
      {(gaps.length > 0 || caveats.length > 0 || result?.note) && (
        <div className="mt-3 max-w-[68ch] border-l-2 border-amber-500/40 pl-3 text-[13px] leading-relaxed text-amber-200/80">
          {gaps.length > 0 && <div>근거가 불완전합니다: {gaps.join(", ")}</div>}
          {caveats.map((c) => (
            <div key={c}>{c}</div>
          ))}
          {result?.note && (
            <div className="text-zinc-400">{String(result.note)}</div>
          )}
        </div>
      )}

      {err && (
        <div className="mt-3 text-sm text-rose-300">
          리포트를 불러오지 못했습니다: {err}
        </div>
      )}
      {!task && !err && (
        <div className="mt-3 text-sm text-zinc-500">불러오는 중…</div>
      )}

      {/* ── A scheduled digest is a report, not a diagnosis ───────────────── */}
      {reportLines && (
        <dl className="mt-4 grid gap-x-8 gap-y-1 sm:grid-cols-2">
          {reportLines.map((l, i) => (
            <div key={i} className="flex justify-between gap-3 text-[13px]">
              <dt className="text-zinc-500">{l.label}</dt>
              <dd className="font-mono text-zinc-200">{l.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {/* ── Three short blocks ────────────────────────────────────────────── */}
      {narrativeParts.length > 0 && (
        <section className="mt-5 max-w-[68ch]">
          <h4 className="text-[13px] font-medium text-zinc-300">평가</h4>
          <div className="mt-1.5 space-y-2 text-[15px] leading-[1.7] text-zinc-200">
            {narrativeParts.map((p, i) => (
              <p key={i}>{p}</p>
            ))}
          </div>
        </section>
      )}

      {obs.length > 0 && (
        <section className="mt-5 max-w-[68ch]">
          <h4 className="text-[13px] font-medium text-zinc-300">
            근거가 된 관측
          </h4>
          <ul className="mt-1.5 space-y-1.5">
            {obs.map((o, i) => (
              <li key={i} className="text-[13px] leading-relaxed">
                <span className="text-zinc-500">
                  {o.when ? fmtClockKo(o.when) : "시각 미기록"}
                </span>{" "}
                <span className="text-zinc-500">
                  {t(categoryLabel(o.category))}
                </span>{" "}
                <span className="font-mono text-[12px] text-zinc-200">
                  {o.pairs
                    .map(
                      ([k, v]) => `${evidenceLabel(k)} ${fmtEvidenceValue(v)}`,
                    )
                    .join(", ")}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {steps.length > 0 && (
        <section className="mt-5 max-w-[68ch]">
          <h4 className="text-[13px] font-medium text-zinc-300">다음 단계</h4>
          {checks.length > 0 && (
            <div className="mt-2">
              <div className="text-[12px] text-sky-300/90">
                확인 (읽기 전용)
              </div>
              <ol className="mt-1 list-decimal space-y-1 pl-5 text-[14px] leading-relaxed text-zinc-200">
                {checks.map((s, i) => (
                  <li key={i}>
                    {s.text}
                    {s.from === "signal" && s.category && (
                      <span className="text-[11px] text-zinc-500">
                        {" "}
                        ({t(categoryLabel(s.category))} {t("신호")})
                      </span>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}
          {changes.length > 0 && (
            <div className="mt-3">
              <div className="text-[12px] text-amber-300/90">
                조치 (변경 작업, 승인 센터를 거칩니다)
              </div>
              <ol className="mt-1 list-decimal space-y-1 pl-5 text-[14px] leading-relaxed text-zinc-200">
                {changes.map((s, i) => (
                  <li key={i}>
                    {s.text}
                    {s.from === "signal" && s.category && (
                      <span className="text-[11px] text-zinc-500">
                        {" "}
                        ({t(categoryLabel(s.category))} {t("신호")})
                      </span>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}
          <p className="mt-2 text-[11px] text-zinc-600">
            분류는 문구를 기준으로 추정합니다. 실행 전에 항목을 직접 확인하세요.
          </p>
        </section>
      )}

      {task && !reportLines && candidates.length === 0 && (
        <div className="mt-4 max-w-[68ch] text-[14px] leading-relaxed text-zinc-400">
          자동 수집 신호에서 순위를 매길 후보를 찾지 못했습니다. 위 범위 한계를
          함께 보고 수동 점검을 권장합니다.
        </div>
      )}

      {/* ── One click away ───────────────────────────────────────────────── */}
      {candidates.length > 1 && (
        <details className="group mt-5 border-t border-zinc-800/60 pt-3">
          <summary className="flex cursor-pointer list-none items-center gap-1 text-[13px] text-zinc-400 transition-colors hover:text-zinc-200 [&::-webkit-details-marker]:hidden">
            <ChevronRight
              size={12}
              className="transition-transform group-open:rotate-90"
            />
            다른 가설 {candidates.length - 1}건
          </summary>
          <ol className="mt-2 flex flex-col gap-3">
            {candidates.slice(1).map((c, i) => (
              <li key={String(c.rank ?? i)} className="flex items-start gap-3">
                <span className="w-5 flex-shrink-0 font-mono text-xs text-zinc-500">
                  #{String(c.rank ?? i + 2)}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="text-[13px] text-zinc-500">
                    {t(categoryLabel(c.category as string | undefined))}
                  </span>{" "}
                  <span className="text-[14px] text-zinc-200">
                    {String(c.summary ?? "")}
                  </span>
                  {/* WHY it ranks lower, in the ranker's own terms: its own
                      score against the leader's, and when it happened. Never a
                      percentage, and never the word confidence. */}
                  <span className="mt-0.5 block font-mono text-[11px] text-zinc-500">
                    순위 점수 {String(c.score ?? "-")}
                    {top?.score !== undefined
                      ? `, 1순위 ${String(top.score)}`
                      : ""}
                    {c.when ? `, ${String(c.when)}` : ""}
                  </span>
                  <RcaCandidateDetail
                    breakdown={c.score_breakdown}
                    evidence={c.evidence}
                    suggestedAction={c.suggested_action}
                  />
                </span>
              </li>
            ))}
          </ol>
        </details>
      )}

      {task && (
        <details className="group mt-3 border-t border-zinc-800/60 pt-3">
          <summary className="flex cursor-pointer list-none items-center gap-1 text-[13px] text-zinc-400 transition-colors hover:text-zinc-200 [&::-webkit-details-marker]:hidden">
            <ChevronRight
              size={12}
              className="transition-transform group-open:rotate-90"
            />
            분석 상세
          </summary>

          {/* The leader's derivation, the one candidate whose score is not in
              the alternatives list above. */}
          {top && (
            <div className="mt-2">
              <div className="font-mono text-[11px] text-zinc-500">
                1순위 순위 점수 {String(top.score ?? "-")}
              </div>
              <RcaCandidateDetail
                breakdown={top.score_breakdown}
                evidence={top.evidence}
                suggestedAction={top.suggested_action}
              />
            </div>
          )}

          <RcaScoringPolicy
            weights={result?.scoring_weights}
            note={result?.scoring_note}
            schemaObservation={result?.schema_observation}
          />

          {result?.signals_examined && (
            <div className="mt-3">
              <div className="text-[11px] text-zinc-500">검사한 신호 수</div>
              <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-[11px] text-zinc-400">
                {Object.entries(result.signals_examined).map(([src, cnt]) => (
                  <span key={src}>
                    {src} {fmtExact(cnt)}
                  </span>
                ))}
              </div>
            </div>
          )}

          {task.trace && task.trace.length > 0 && (
            <div className="mt-3">
              <div className="flex items-center gap-3">
                <span className="text-[11px] text-zinc-500">실행 추적</span>
                {task.duration_ms != null && (
                  <span className="font-mono text-[11px] text-zinc-500">
                    총 {(Number(task.duration_ms) / 1000).toFixed(1)}s
                  </span>
                )}
              </div>
              <ol className="mt-1 flex flex-col gap-0.5">
                {task.trace.map((s, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-2 font-mono text-[11px]"
                  >
                    <span className="w-4 flex-shrink-0 text-zinc-600">
                      {i + 1}.
                    </span>
                    <span className="flex-shrink-0 text-zinc-300">
                      {s.step}
                    </span>
                    <span className="flex-shrink-0 text-zinc-500">
                      {s.tool}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-zinc-500">
                      {s.detail}
                    </span>
                    <span className="flex-shrink-0 text-zinc-600">
                      {String(s.ms)}ms
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </details>
      )}
    </div>
  );
}
