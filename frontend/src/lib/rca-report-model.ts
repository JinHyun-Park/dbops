/**
 * The RCA report's data shaping, kept out of the component so it can be run.
 *
 * Everything here is pure and label-free: the Korean labels for a category and
 * for an evidence key already live in rca-candidate-detail.tsx (which is JSX),
 * so this module hands back RAW keys and lets the renderer label them. That is
 * what keeps `node tools/rca-report-check.mjs` able to load this file directly.
 *
 * The one rule none of these functions may break: never make the report look
 * more certain than the system is. A ranking is an investigation order, so no
 * function here turns a score into a confidence, and stepKind() is biased so a
 * change can never be presented with a read-only check's implied safety.
 */

import type { AgentTask } from "@/lib/api-client";

export type RcaResult = NonNullable<AgentTask["result"]>;
export type RcaCandidate = NonNullable<RcaResult["candidates"]>[number];

// diagnose_root_cause's own source names, from its pre-seeded `examined` dict
// and its `skipped` appends. A name that is not here falls through to itself,
// so a source added later stays readable instead of being dropped.
const SOURCE_LABEL: Record<string, string> = {
  schema_changes: "스키마 변경 이력",
  schema_changes_read_error: "스키마 변경 이력 (읽기 실패)",
  schema_changes_probe_error: "스키마 변경 이력 (수집기 확인 실패)",
  schema_changes_observation_error: "스키마 변경 이력 (관측 범위 확인 실패)",
  schema_changes_unmigrated: "스키마 변경 이력 (수집 테이블 없음)",
  schema_changes_unconfirmed_schemas: "스키마 변경 이력 (관측 범위 미확인)",
  schema_changes_unsupported_engine: "스키마 변경 이력 (이 엔진은 미지원)",
  events: "이벤트 로그",
  blocking: "락 경합",
  metric_spikes: "메트릭 급증",
  counter_spikes: "엔진 카운터",
  slow_queries: "슬로우 쿼리",
  elasticache_signals: "ElastiCache 신호",
  engine_family: "엔진 패밀리 (미확인, 메트릭 집합은 추정값)",
};

export function sourceLabel(key: string): string {
  return SOURCE_LABEL[key] ?? key;
}

/**
 * Material coverage GAPS, which are not source counts.
 *
 * "blocking 데이터 없음" changes what the conclusion is worth, so it belongs
 * beside the conclusion. "blocking: 0" as a number does not, and stays in the
 * details tier with the rest of `signals_examined`.
 */
export function coverageGaps(result: RcaResult): string[] {
  const gaps: string[] = [];
  const skipped = (result.skipped_sources ?? []).map(String);
  for (const s of skipped) gaps.push(`${sourceLabel(s)} 확인 불가`);
  // A skipped source already reported its own gap, including the schema
  // reasons that suffix the source name (schema_changes_unmigrated and
  // friends). Saying "데이터 없음" for it too would repeat the same fact in
  // the weaker of the two wordings.
  for (const [src, count] of Object.entries(result.signals_examined ?? {})) {
    const alreadySaid = skipped.some(
      (s) => s === src || s.startsWith(`${src}_`),
    );
    if (Number(count) === 0 && !alreadySaid) {
      gaps.push(`${sourceLabel(src)} 데이터 없음`);
    }
  }
  return gaps;
}

export interface Headline {
  /**
   * What the report's first two seconds are ALLOWED to claim.
   *   "digest":     a scheduled health digest. It ranked nothing, so it may not
   *                 be framed as a hypothesis and carries no ranking caveat.
   *   "hypothesis": a ranked leader, from the report or from the list row's
   *                 projection of it.
   *   "none":       nothing was ranked. Say that instead of promoting whatever
   *                 free text happens to be on the row.
   */
  kind: "digest" | "hypothesis" | "none";
  text: string;
}

/**
 * The headline, and the only place that decides what KIND of claim it is.
 *
 * `row.summary` is deliberately absent from the hypothesis chain. It is free
 * text the worker writes for every kind of task, so letting it fall through
 * printed a scheduled digest ("헬스 다이제스트: healthy") as 유력 가설 under a
 * caveat about a ranking that never ran. The /tasks list row never did that,
 * and the two surfaces must not disagree about the same row.
 */
export function reportHeadline(
  row: Pick<AgentTask, "kind" | "summary" | "finding">,
  top: RcaCandidate | undefined,
): Headline {
  // The kind the worker dispatched on, so this holds for a future digest that
  // carries no `lines`, and is known from the list row before the full read.
  if (row.kind === "scheduled_report") {
    return { kind: "digest", text: String(row.summary ?? "") };
  }
  const text =
    (top?.summary as string | undefined) || row.finding?.summary || "";
  return text ? { kind: "hypothesis", text } : { kind: "none", text: "" };
}

/** The two facts from the top candidate's breakdown that change what the
 *  reader should do, so they are hoisted next to the headline instead of
 *  staying behind a click. RcaCandidateDetail shows the same two per row. */
export function topCaveats(top: RcaCandidate | undefined): string[] {
  const b = top?.score_breakdown;
  if (!b) return [];
  const out: string[] = [];
  if (b.lone_sample === true) {
    out.push("1순위 근거가 구간 최댓값 단일 샘플에 의존합니다");
  }
  if (b.qualified_by === "peak") {
    out.push("구간 평균이 아니라 최댓값으로 판정된 신호입니다");
  }
  return out;
}

/**
 * A next step is either a read-only CHECK that establishes whether the
 * hypothesis holds, or an operational CHANGE. They must not render as one list:
 * a rollback would inherit a verification query's implied safety.
 *
 * ponytail: keyword classification, with a deliberate fail-safe bias. Two
 * shapes arrive here: the model's Korean `recommendations`, where the verb is
 * at the END and containment is the only thing that works, and a collector's
 * English `suggested_action`, which is verb-first (see CHECK_LEAD below). In
 * both, a change word beats a check word and an unclassifiable step lands in
 * 조치, so a change can never be labelled read-only. Upgrade path when this
 * misfiles too often: have the producer emit the class (the collectors write
 * `suggested_action`, so they already know), and keep this only for the
 * model's free-text `recommendations`.
 */
const CHANGE_WORDS = [
  "롤백",
  "되돌",
  "재시작",
  "재부팅",
  "재구성",
  "중단",
  "종료",
  "취소",
  "kill",
  "변경",
  "수정",
  "적용",
  "조정",
  "확장",
  "축소",
  "증설",
  "스케일",
  "추가",
  "생성",
  "삭제",
  "제거",
  "재색인",
  "reindex",
  "vacuum",
  "failover",
  "페일오버",
  "승인",
  "배포",
  // The English write verbs this platform actually has tools for. Without them
  // the lead-clause guard in stepKind() would have nothing to catch an English
  // change step with.
  "rollback",
  "roll back",
  "restart",
  "reboot",
  "terminate",
  "resize",
  "scale",
  "modify",
  "drop",
  "truncate",
  "delete",
  "remove",
  "deploy",
  "upgrade",
  "migrate",
  "enable",
  "disable",
];

const CHECK_WORDS = [
  "확인",
  "점검",
  "조회",
  "검토",
  "모니터",
  "관측",
  "분석",
  "파악",
  "살펴",
  "비교",
  "측정",
  "진단",
  "추적",
  "검증",
  "explain",
  // The one English check verb the producer uses MID-sentence, where the
  // verb-first rule below cannot see it: "... treat any nonzero window as
  // real. Check capacity/limits (throttling) ...". A change word still wins
  // first, so this cannot turn a change into a read-only step.
  "check",
];

/**
 * A CHECK verb leading an English step. Containment cannot see this: every
 * deterministic `suggested_action` diagnose_root_cause emits is English and
 * VERB-FIRST, and names a change in the rationale clause that follows the
 * first ";" or ":" or "(" without asking for one ("Inspect the event detail;
 * failover/reboot/OOM events often explain ...", "Review the schema diff;
 * ... consider a rollback."). So the lead clause's own verb decides.
 */
const CHECK_LEAD =
  /^(?:review|inspect|identify|investigate|examine|explain|check|verify|confirm|compare|monitor|measure|diagnose|trace|analy[sz]e|assess|look|audit)\b/;

export function stepKind(text: string): "check" | "change" {
  const t = text.toLowerCase();
  // The lead clause only: everything after the first ";" / ":" / "(" is
  // rationale, and a noun there is not an instruction. The fail-safe bias is
  // kept INSIDE the lead, so a change word there still beats the check verb
  // ("Check write load / failover" stays a 조치, the conservative direction).
  const lead = t.split(/[;:(]/, 1)[0];
  if (CHECK_LEAD.test(lead) && !CHANGE_WORDS.some((w) => lead.includes(w))) {
    return "check";
  }
  if (CHANGE_WORDS.some((w) => t.includes(w))) return "change";
  if (CHECK_WORDS.some((w) => t.includes(w))) return "check";
  return "change";
}

export interface Step {
  text: string;
  kind: "check" | "change";
  /** The model's headline advice, or a collector's per-signal next step. */
  from: "model" | "signal";
  /** Raw candidate category for a per-signal step; the renderer labels it. */
  category?: string;
}

/**
 * Both advice sources, in reading order. The model's `recommendations` are the
 * headline advice and come first; each candidate's `suggested_action` is that
 * signal's own next step.
 *
 * The backend dedupes `recommendations` against EACH OTHER only, never against
 * a per-candidate action: those actions are written in English by the
 * collectors while the model answers in Korean, so no lexical matcher can pair
 * them and a cross-language guess could drop a genuinely different
 * instruction. See task_worker._dedupe_advice. So the exact-text `seen` guard
 * below is the only thing standing between the two lists here, and it is
 * enough in practice for the same reason: the two lists are not in the same
 * language. Both survive on purpose; a signal with no next step reads worse
 * than a repeated bullet.
 */
export function nextSteps(result: RcaResult): Step[] {
  const out: Step[] = [];
  const seen = new Set<string>();
  const push = (raw: unknown, from: "model" | "signal", category?: string) => {
    const text = String(raw ?? "").trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    out.push({ text, kind: stepKind(text), from, category });
  };
  for (const rec of result.recommendations ?? []) push(rec, "model");
  for (const c of result.candidates ?? []) {
    push(c.suggested_action, "signal", c.category as string | undefined);
  }
  return out;
}

/** Sentence split that tolerates Korean prose ending in a plain period. */
export function sentences(text: string): string[] {
  return text
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function normalize(s: string): string {
  return s.replace(/\s+/g, "").replace(/[.,()[\]]/g, "");
}

/**
 * The assessment paragraphs, minus an opening sentence that only restates the
 * headline the reader just read one line above. Conservative: only an outright
 * containment match either way counts, so a first sentence that adds anything
 * survives, and a single-sentence narrative is never emptied.
 */
export function assessment(narrative: string, headline: string): string[] {
  const parts = sentences(narrative);
  if (parts.length > 1 && headline) {
    const lead = normalize(parts[0]);
    const head = normalize(headline);
    if (lead && head && (lead.includes(head) || head.includes(lead))) {
      return parts.slice(1);
    }
  }
  return parts;
}

// The measurements worth putting in front of the reader, in the order they read
// as a sentence. Everything else in `evidence` stays in the details tier.
const OBSERVATION_KEYS = [
  "metric_type",
  "window_avg",
  "baseline_avg",
  "ratio",
  "window_max",
  "peak_ratio",
  "blocked_sessions",
  "max_wait_sec",
  "calls",
  "mean_time_ms",
  "total_time_ms",
  "schema_name",
];

export interface Observation {
  /** The observation's OWN timestamp, or null when the signal carried none. */
  when: string | null;
  /** Raw category; the renderer labels it. */
  category?: string;
  /** Raw [key, value] measurements, straight off `evidence`. */
  pairs: [string, unknown][];
}

/**
 * Concrete measurements with their timestamps, out of `evidence`. Not the
 * summary prose: the prose is the claim, these are what was measured. Capped at
 * three, because this block exists to be read, not to be complete.
 */
export function observations(candidates: RcaCandidate[]): Observation[] {
  const out: Observation[] = [];
  for (const c of candidates) {
    const ev = c.evidence;
    if (!ev || typeof ev !== "object") continue;
    const pairs = OBSERVATION_KEYS.filter((k) => ev[k] !== undefined)
      .slice(0, 4)
      .map((k) => [k, ev[k]] as [string, unknown]);
    if (pairs.length === 0) continue;
    const when =
      (typeof c.when === "string" && c.when) ||
      (typeof ev.snapshot_time === "string" && ev.snapshot_time) ||
      null;
    out.push({ when, category: c.category as string | undefined, pairs });
    if (out.length === 3) break;
  }
  return out;
}
