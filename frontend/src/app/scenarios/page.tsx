"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Play, Loader2, ArrowRight } from "lucide-react";
import {
  fetchScenarioRuns,
  fetchScenarios,
  runScenario,
  type ScenarioCatalog,
  type ScenarioRun,
} from "@/lib/api-client";
import {
  EmptyState,
  PageBody,
  PageHeader,
  Section,
} from "@/components/design-system/page-shell";
import { categoryLabel } from "@/components/rca/rca-candidate-detail";
import { fmtRelative } from "@/lib/format";
import { useT } from "@/lib/i18n";

// Demo page: press a button, a realistic incident appears in the signal tables,
// and the SAME automatic RCA that runs for a real alert explains it. The point
// of the page is that the explanation is not scripted, so the copy says what is
// synthetic (the symptom) and what is not (everything after it).
//
// It deliberately does NOT touch the target database. Every registered cluster
// here is a shared read-only fixture, and a demo button that can saturate one is
// an outage with a nicer UI.

const STATUS_STYLE: Record<string, string> = {
  running: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  injected: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  resolved: "bg-zinc-700/30 text-zinc-400 border-zinc-600",
};
const STATUS_LABEL: Record<string, string> = {
  running: "주입 중",
  injected: "RCA 진행",
  resolved: "정리 완료",
};

export default function ScenariosPage() {
  const t = useT();
  const [catalog, setCatalog] = useState<ScenarioCatalog | null>(null);
  const [runs, setRuns] = useState<ScenarioRun[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const router = useRouter();

  const loadRuns = useCallback(async () => {
    try {
      const res = await fetchScenarioRuns();
      setRuns(res.runs ?? []);
    } catch {
      // History is supporting detail. A failure here must not blank the page
      // the buttons live on, so it is swallowed and the list stays as-is.
    }
  }, []);

  // Both initial reads are promise chains guarded by `alive`, not a call to
  // loadRuns(): react-hooks/set-state-in-effect rejects invoking a function
  // that sets state from an effect body, and it is right to, because the
  // unmount path then has nothing to cancel against. loadRuns stays for the
  // post-run refresh, which happens in an event handler rather than an effect.
  useEffect(() => {
    let alive = true;
    fetchScenarios()
      .then((c) => {
        if (alive) setCatalog(c);
      })
      .catch((e: Error) => {
        if (alive) setLoadError(e.message);
      });
    fetchScenarioRuns()
      .then((res) => {
        if (alive) setRuns(res.runs ?? []);
      })
      .catch(() => {
        // History is supporting detail: a failure here must not blank the page
        // the buttons live on.
      });
    return () => {
      alive = false;
    };
  }, []);

  const onRun = useCallback(
    async (id: string) => {
      setBusy(id);
      setError(null);
      setNotice(null);
      try {
        const res = await runScenario(id);
        if (res.rca_enqueued) {
          setNotice(
            t(
              "신호를 주입했습니다. 자동 RCA가 큐에 등록되었습니다 (작업 {n}). 분석은 보통 수십 초 안에 끝납니다.",
            ).replace("{n}", String(res.task_id)),
          );
        } else {
          // The signals landed but no task exists, so there is no report
          // coming. Saying so beats a spinner that never resolves.
          setNotice(
            t(
              "신호는 주입했지만 자동 RCA를 큐에 넣지 못했습니다. 에이전트 작업 테이블 설정을 확인하세요.",
            ),
          );
        }
        await loadRuns();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setBusy(null);
      }
    },
    [loadRuns],
  );

  const disabled = !catalog?.enabled;

  return (
    <PageBody>
      <PageHeader
        eyebrow={t("demo")}
        title={t("장애 시나리오")}
        description={t(
          "버튼을 누르면 실제와 같은 장애 신호가 주입되고, 실제 자동 RCA가 원인과 조치를 분석합니다.",
        )}
      />
      {loadError && (
        <div className="px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {loadError}
        </div>
      )}

      {/* What is real and what is not. A demo that does not say this invites
            the reasonable assumption that the incident is also fabricated. */}
      <Section title={t("동작 방식")}>
        <div className="flex flex-col gap-1.5 text-xs text-zinc-400">
          <div>
            {t(
              "시나리오는 캐시된 신호 테이블(metric_snapshots, event_log, blocking_locks, query_stats, schema_snapshots)에 해당 장애가 관측되었을 때와 같은 행을 기록합니다.",
            )}
          </div>
          <div>
            {t(
              "그 다음은 전부 실제 경로입니다. 동일한 결정론적 랭커가 동일한 가중치로 신호를 채점하고, 동일한 모델 호출이 한국어 원인 설명과 권장 조치를 생성합니다.",
            )}
          </div>
          <div className="text-zinc-500">
            {t(
              "대상 데이터베이스는 건드리지 않습니다. 주입된 행은 RCA 분석 구간({n}분)에서 벗어나면 자동으로 정리됩니다. 한 번에 하나의 시나리오만 실행됩니다.",
            ).replace("{n}", String(catalog?.window_minutes ?? 30))}
          </div>
        </div>
      </Section>

      {disabled && catalog && (
        <div className="flex items-start gap-2 px-3 py-2 border border-amber-500/40 bg-amber-500/10 text-amber-200 text-xs">
          <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
          <span>
            {t(
              "대상 클러스터가 설정되지 않아 실행이 비활성화되어 있습니다. cdk/config/settings.py의 SCENARIO_CLUSTER_ID에 등록된 클러스터를 지정하고 agent 스택을 재배포하세요.",
            )}
          </span>
        </div>
      )}

      {notice && (
        <div className="flex items-start gap-2 px-3 py-2 border border-emerald-500/40 bg-emerald-500/10 text-emerald-200 text-xs">
          <span>{notice}</span>
          <button
            onClick={() => router.push("/tasks")}
            className="ml-auto inline-flex items-center gap-1 flex-shrink-0 text-emerald-200 hover:text-emerald-100"
          >
            {t("작업으로 이동")}
            <ArrowRight size={12} />
          </button>
        </div>
      )}
      {error && (
        <div className="px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {error}
        </div>
      )}

      <Section
        title={t("시나리오")}
        description={
          catalog?.cluster_id
            ? t("대상 클러스터: {n}").replace("{n}", catalog.cluster_id)
            : undefined
        }
      >
        {!catalog ? (
          <div className="text-xs text-zinc-500">{t("불러오는 중...")}</div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {catalog.scenarios.map((s) => (
              <div
                key={s.id}
                className="flex flex-col gap-2 border border-zinc-800 bg-zinc-950 p-4"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-sm text-zinc-100">{s.title}</div>
                    <div className="text-xs text-zinc-500 mt-0.5">
                      {s.summary}
                    </div>
                  </div>
                  {/* The category and its base weight, so a viewer can tell
                        BEFORE running it why this scenario's cause will rank
                        where it does. The ordering is a stated policy. */}
                  <span
                    className="flex-shrink-0 text-[10px] px-1.5 py-0.5 border border-zinc-700 text-zinc-400"
                    title={t("랭킹 카테고리 {c}, 기본 가중치 {w}")
                      .replace("{c}", s.category)
                      .replace("{w}", String(s.weight))}
                  >
                    {t(categoryLabel(s.category))} {s.weight}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1">
                  {s.signals.map((sig) => (
                    <span
                      key={sig}
                      className="text-[10px] font-mono px-1.5 py-0.5 border border-zinc-800 text-zinc-500"
                    >
                      {sig}
                    </span>
                  ))}
                </div>
                <button
                  onClick={() => onRun(s.id)}
                  disabled={disabled || busy !== null}
                  className="mt-1 inline-flex items-center justify-center gap-1.5 text-xs px-3 py-1.5 border border-zinc-700 text-zinc-300 hover:border-amber-500/50 hover:text-amber-200 disabled:opacity-40 disabled:hover:border-zinc-700 disabled:hover:text-zinc-300 transition-colors"
                >
                  {busy === s.id ? (
                    <>
                      <Loader2 size={12} className="animate-spin" />
                      {t("주입 중")}
                    </>
                  ) : (
                    <>
                      <Play size={12} />
                      {t("실행")}
                    </>
                  )}
                </button>
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section title={t("실행 이력")}>
        {runs.length === 0 ? (
          <EmptyState
            title={t("실행 이력 없음")}
            description={t(
              "위에서 시나리오를 실행하면 여기에 기록되고, 각 실행에서 생성된 RCA 리포트로 바로 이동할 수 있습니다.",
            )}
          />
        ) : (
          <div className="flex flex-col divide-y divide-zinc-800/60">
            {runs.map((r) => (
              <div key={r.id} className="flex items-center gap-3 py-2 text-xs">
                <span
                  className={`flex-shrink-0 px-1.5 py-0.5 border ${
                    STATUS_STYLE[r.status] ?? "border-zinc-700 text-zinc-400"
                  }`}
                >
                  {t(STATUS_LABEL[r.status] ?? r.status)}
                </span>
                <span className="text-zinc-200 flex-1 min-w-0 truncate">
                  {r.title ?? r.scenario_id}
                </span>
                <span className="text-zinc-500 font-mono flex-shrink-0">
                  {fmtRelative(r.started_at)}
                </span>
                {r.task_id ? (
                  <button
                    onClick={() =>
                      router.push(
                        `/tasks?focus=${encodeURIComponent(r.task_id!)}`,
                      )
                    }
                    className="flex-shrink-0 inline-flex items-center gap-1 text-zinc-400 hover:text-amber-200 transition-colors"
                  >
                    {t("RCA 보기")}
                    <ArrowRight size={11} />
                  </button>
                ) : (
                  <span className="flex-shrink-0 text-zinc-600">
                    {t("RCA 없음")}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>
    </PageBody>
  );
}
