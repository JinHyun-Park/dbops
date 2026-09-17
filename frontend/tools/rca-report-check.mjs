#!/usr/bin/env node
/**
 * The one runnable check behind the RCA report's data shaping.
 *
 * Run: node tools/rca-report-check.mjs   (or: npm run rca:check)
 *
 * Node loads the .ts module directly (type stripping), so there is no test
 * framework, no runner config and no build step here. Assertions only, over the
 * behaviours that are wrong in a way tsc and the build cannot see:
 *
 *  1. stepKind() must never label an operational CHANGE as a read-only check.
 *     That is the one misclassification with a real cost: it would hand a
 *     rollback a verification query's implied safety. And it must classify what
 *     the PRODUCER actually emits, which is English and verb-first, not the
 *     Korean fixtures this check used to be written entirely in.
 *  2. coverageGaps() must report a MATERIAL gap ("blocking 데이터 없음") and
 *     must not report the same gap twice when the source was also skipped.
 *  3. assessment() must drop a narrative lead that only restates the headline,
 *     and must never empty a narrative that says something else.
 *  4. topCaveats() must surface lone_sample and qualified_by=peak. They are the
 *     single-sample peak path's own discount made visible, and nothing else on
 *     the surface says that the leader stands on one datapoint.
 *  5. reportHeadline() must not call anything a hypothesis that was never
 *     ranked. A scheduled health digest is a digest, and a report with no
 *     candidates has no headline at all.
 *  6. narrativeLanguage() must name the language a STORED narrative is in, so
 *     the report can tell a cross-locale reader what they are looking at. The
 *     stamp wins over the script of the prose, and a report with no narrative
 *     must claim nothing.
 */
import assert from "node:assert/strict";
import {
  assessment,
  coverageGaps,
  nextSteps,
  observations,
  reportHeadline,
  stepKind,
  sourceLabel,
  topCaveats,
} from "../src/lib/rca-report-model.ts";
import { narrativeLanguage } from "../src/lib/narrative-language.ts";

// 1. Classification, biased so a change can never read as read-only.
assert.equal(stepKind("현재 blocking 세션을 확인하세요"), "check");
assert.equal(stepKind("EXPLAIN으로 실행계획을 비교하세요"), "check");
assert.equal(stepKind("변경된 파라미터를 롤백하세요"), "change");
assert.equal(stepKind("리더 인스턴스를 재시작하세요"), "change");
// A sentence carrying both words is a change: "인덱스 추가를 검토하세요"
// proposes a change, and the safe direction is to say so.
assert.equal(stepKind("인덱스 추가를 검토하세요"), "change");
// Unclassifiable falls to 조치, never to the read-only bucket.
assert.equal(stepKind("운영팀과 협의"), "change");

// 1b. THE REAL PRODUCER STRINGS, verbatim out of
// mcp-servers/mcp_servers/incident/tools/diagnose_root_cause.py (the
// {metric_type} f-string filled in). Every deterministic suggested_action is
// ENGLISH and verb-first, and four of the five name a change in the rationale
// clause after the first ";" or "(" ("consider a rollback",
// "failover/reboot/OOM", "a terminated session", "load change") while asking
// for nothing but a read. Korean fixtures cannot see that, which is how four
// read-only steps came to be filed under 조치 with its approval-flow promise.
for (const text of [
  "Review the schema diff; correlate the deploy/migration with the symptom onset and consider a rollback.",
  "Inspect the event detail; failover/reboot/OOM events often explain abrupt connection or latency changes.",
  "Identify the blocking transaction; long blocking chains may need a terminated session or an index/query fix.",
  "Investigate what drove cpu_utilization up around this window (load change, plan regression, runaway query).",
  "EXPLAIN the query; check for a missing index or a plan regression coinciding with the incident.",
]) {
  assert.equal(stepKind(text), "check", text);
}

// 1b2. The producer's other two next-step shapes, also verbatim: the
// counter_spike conditional and the two elasticache branches. Their check verb
// is MID-sentence, not leading, so CHECK_WORDS has to carry them.
for (const text of [
  "cpu_utilization is an event counter that is normally 0, so treat any nonzero window as real. Check capacity/limits (throttling), long-lived cursors or memory pressure depending on the metric.",
  "cpu_utilization rose sharply above its usual level; check capacity/limits around this window.",
  "Memory pressure: evictions spiking suggests the working set exceeds capacity; check maxmemory-policy and node size.",
]) {
  assert.equal(stepKind(text), "check", text);
}
// The one deterministic step that stays a 조치, and on purpose: its own lead
// clause names a failover, so the conservative reading wins over the fact that
// it asks only for a read.
assert.equal(
  stepKind(
    "Check write load / failover; replication lag often coincides with a primary failover or load surge.",
  ),
  "change",
);

// 1c. The fail-safe bias holds on those same English shapes: a lead clause that
// asks for the change is a change, and an English step with no verb to read
// still lands in 조치.
assert.equal(
  stepKind("Roll back the migration and restart the writer."),
  "change",
);
assert.equal(stepKind("Review the plan, then resize the instance."), "change");
assert.equal(
  stepKind(
    "Memory pressure: evictions suggest the working set exceeds capacity.",
  ),
  "change",
);

// 2. Gaps, which are not counts.
const gaps = coverageGaps({
  signals_examined: { blocking: 0, events: 2, schema_changes: 0 },
  skipped_sources: ["metric_spikes", "schema_changes_unmigrated"],
});
// Asserted on the STRUCTURE, because coverageGaps no longer pre-composes a
// Korean sentence: rca-report.tsx rendered that string raw, so an English
// operator read every coverage gap in Korean. The producer now returns
// {source, kind} and the consumer looks the label up and translates it.
const shown = (g) => `${g.source}/${g.kind}`;
const seen = gaps.map(shown).join(" | ");
assert.ok(
  gaps.some((g) => g.source === "blocking" && g.kind === "nodata"),
  seen,
);
assert.ok(
  gaps.some((g) => g.source === "metric_spikes" && g.kind === "unchecked"),
  seen,
);
// events had rows, so it is not a gap at all.
assert.ok(!gaps.some((g) => g.source === "events"), seen);
// schema_changes was skipped WITH a reason, so the weaker "nodata" kind must
// not be added on top of it.
assert.equal(
  gaps.filter((g) => g.source.startsWith("schema_changes")).length,
  1,
  seen,
);
// The label lookup the consumer depends on still resolves, and an UNKNOWN
// source falls through to its raw identifier rather than disappearing.
assert.equal(sourceLabel("blocking"), "락 경합");
assert.equal(sourceLabel("something_new"), "something_new");

// 3. Narrative lead dedupe.
const headline = "orders 테이블에 인덱스가 추가되었습니다";
assert.deepEqual(
  assessment(`${headline}. 그 직후 CPU가 상승했습니다.`, headline),
  ["그 직후 CPU가 상승했습니다."],
);
assert.deepEqual(
  assessment("커넥션이 급증했습니다. CPU도 함께 올랐습니다.", headline),
  ["커넥션이 급증했습니다.", "CPU도 함께 올랐습니다."],
);
// A single sentence is never emptied, even when it echoes the headline.
assert.deepEqual(assessment(`${headline}.`, headline), [`${headline}.`]);

// 4. Both advice sources survive, model list first, exact repeats dropped.
const steps = nextSteps({
  recommendations: ["인덱스를 롤백하세요", "인덱스를 롤백하세요"],
  candidates: [
    { category: "schema_change", suggested_action: "변경 이력을 확인하세요" },
  ],
});
assert.deepEqual(
  steps.map((s) => [s.text, s.kind, s.from]),
  [
    ["인덱스를 롤백하세요", "change", "model"],
    ["변경 이력을 확인하세요", "change", "signal"],
  ],
);

// 4b. The exact-text guard now spans the two lists, which is what changed when
// the narrative started following the operator's locale. The collectors always
// write English `suggested_action`; on an English task the model's
// `recommendations` are English too, so a genuine repeat is byte-identical and
// collapses here. The backend still does NOT compare the lists (it would drop
// the evidence-bound side), so this is the only guard, and it is exact, so it
// can never eat a different instruction.
const dupe =
  "Review the schema diff; a recent index change often explains this";
assert.deepEqual(
  nextSteps({
    recommendations: [dupe],
    candidates: [{ category: "schema_change", suggested_action: dupe }],
  }).map((s) => [s.text, s.kind, s.from]),
  [[dupe, "check", "model"]],
);
// Different English advice from the two lists both survive, and the read-only
// check never inherits the change step's kind.
const both = nextSteps({
  recommendations: ["Raise work_mem to 16MB"],
  candidates: [{ category: "schema_change", suggested_action: dupe }],
});
assert.deepEqual(
  both.map((s) => [s.kind, s.from]),
  [
    ["change", "model"],
    ["check", "signal"],
  ],
);

// 5. Observations carry their own timestamp and cap at three.
const obs = observations([
  {
    when: "2026-09-15T19:54:00+00:00",
    category: "metric_spike",
    evidence: { metric_type: "cpu", window_avg: 82.4 },
  },
  {
    category: "blocking",
    evidence: {
      snapshot_time: "2026-09-15T19:50:00+00:00",
      blocked_sessions: 4,
    },
  },
  { category: "event", evidence: { calls: 10 } },
  { category: "slow_query", evidence: { calls: 3 } },
]);
assert.equal(obs.length, 3);
assert.equal(obs[0].when, "2026-09-15T19:54:00+00:00");
assert.equal(obs[1].when, "2026-09-15T19:50:00+00:00");
assert.equal(obs[2].when, null);
assert.deepEqual(obs[0].pairs, [
  ["metric_type", "cpu"],
  ["window_avg", 82.4],
]);
// A candidate with no evidence contributes no observation.
assert.deepEqual(observations([{ category: "event" }]), []);

// 6. The leader's two qualifications reach the headline. diagnose_root_cause
// sets both on the single-sample peak path, where the score has ALREADY been
// discounted by LONE_PEAK_CONFIDENCE because one datapoint cannot be told apart
// from a bad reading. Dropping them silently is what makes the report look more
// certain than the system is, and nothing else on the surface says it.
const caveats = topCaveats({
  score_breakdown: {
    base_weight: 3.0,
    recency_factor: 0.9,
    qualified_by: "peak",
    lone_sample: true,
    formula: "base × recency × spike_magnitude × lone_sample_confidence",
  },
});
assert.equal(caveats.length, 2, caveats.join(" | "));
assert.ok(
  caveats.some((c) => c.includes("단일 샘플")),
  caveats.join(" | "),
);
assert.ok(
  caveats.some((c) => c.includes("최댓값으로 판정")),
  caveats.join(" | "),
);
// A sustained elevation claims neither, and neither does a candidate with no
// breakdown at all: silence here means "not qualified", so it may not be faked.
assert.deepEqual(
  topCaveats({
    score_breakdown: { qualified_by: "average", lone_sample: false },
  }),
  [],
);
assert.deepEqual(topCaveats({ score_breakdown: {} }), []);
assert.deepEqual(topCaveats(undefined), []);

// 7. What the headline is ALLOWED to claim. A scheduled digest ranked nothing,
// so framing its summary as 유력 가설 with "순위는 조사 우선순위" underneath
// describes a ranking that never ran. The /tasks list row already gets this
// right (rowHeadline emits no prefix when `finding` is null), and the two
// surfaces must agree about the same row.
assert.deepEqual(
  reportHeadline(
    {
      kind: "scheduled_report",
      summary: "헬스 다이제스트: healthy",
      finding: null,
    },
    undefined,
  ),
  { kind: "digest", text: "헬스 다이제스트: healthy" },
);
// An RCA that ranked nothing has NO headline: `row.summary` is the worker's
// free text for any task kind, so it may not be promoted to a hypothesis.
assert.deepEqual(
  reportHeadline(
    {
      kind: "auto_rca",
      summary: "자동 수집 신호에서 뚜렷한 원인 미발견, 수동 점검 권장",
      finding: null,
    },
    undefined,
  ),
  { kind: "none", text: "" },
);
// The two ranked sources, the report's own leader first.
assert.deepEqual(
  reportHeadline(
    { kind: "auto_rca", summary: "무시되어야 하는 요약", finding: null },
    { summary: "cpu_utilization 급증" },
  ),
  { kind: "hypothesis", text: "cpu_utilization 급증" },
);
assert.deepEqual(
  reportHeadline(
    {
      kind: "manual_rca",
      summary: "무시되어야 하는 요약",
      finding: {
        category: "schema_change",
        summary: "orders 테이블 스키마 변경",
        candidate_count: 3,
      },
    },
    undefined,
  ),
  { kind: "hypothesis", text: "orders 테이블 스키마 변경" },
);

// 8. narrativeLanguage(): what the cross-locale label is allowed to claim.
// A fleet-wide inbox holds reports in both languages, and a wrong label is a
// false statement on the report surface, so the stamp must win and a report
// with no prose must claim nothing at all.
assert.equal(
  narrativeLanguage({ narrative: "CPU 사용률이 급증한 것으로 보입니다." }),
  "ko",
);
assert.equal(
  narrativeLanguage({ narrative: "CPU utilization appears to have spiked." }),
  "en",
);
// The stamp is authoritative: task_worker writes it from the locale it actually
// generated in, and an English narrative that quotes a Korean parameter note
// must still read as English.
assert.equal(
  narrativeLanguage({
    narrative: 'CPU appears to have spiked; the note says "단일 샘플".',
    narrative_locale: "en",
  }),
  "en",
);
assert.equal(
  narrativeLanguage({ narrative: "Looks English.", narrative_locale: "ko" }),
  "ko",
);
// A stamp from some future release is not one of the two console languages, so
// it falls through to the prose rather than being trusted or rendered.
assert.equal(
  narrativeLanguage({ narrative: "급증했습니다.", narrative_locale: "ja" }),
  "ko",
);
// No narrative, no label. A digest and a report whose model call failed both
// land here, and neither may be labelled.
for (const empty of [
  {},
  null,
  undefined,
  { narrative: "" },
  { narrative: "   " },
  { narrative_locale: "en" },
]) {
  assert.equal(narrativeLanguage(empty), null, JSON.stringify(empty));
}

console.log("rca-report-check: ok");
