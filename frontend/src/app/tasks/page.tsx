"use client";

/**
 * /tasks: THE RCA INBOX, and the only destination for a finished analysis.
 *
 * The complaint this is built against, verbatim: "I have to go into every
 * cluster one by one to see the RCA reports. There is no visibility at all into
 * when the latest content arrived or what is new. And the RCA content itself is
 * not readable."
 *
 * So, three things:
 *  1. ONE canonical destination, fleet-wide by DEFAULT. This page used to open
 *     filtered to the globally-selected cluster, which is exactly the
 *     one-cluster-at-a-time view the operator was complaining about. The scope
 *     is now an explicit control that starts at every cluster, independent of
 *     the global selector, and a link carrying ?cluster= still opens here with
 *     that filter applied visibly.
 *  2. "NEW" MEANS A READABLE REPORT ARRIVED: `published_at` and nothing else.
 *     See @/lib/tasks-watermark for what it is never allowed to mean. Report
 *     time and incident time are DIFFERENT CLOCKS and both are on the row.
 *  3. The report itself is a reading order, not seven equal sections. See
 *     @/components/rca/rca-report.
 *
 * Not grouped into per-cluster sections on purpose: eleven expanded groups is
 * eleven places to scan, which is the complaint restated as a layout.
 * Chronology gives the visibility; the cluster on each row and the scope
 * control give the structure.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createSchedule,
  createTask,
  deleteSchedule,
  fetchClusters,
  fetchSchedules,
  fetchTaskStats,
  fetchTasks,
  type AgentSchedule,
  type AgentTask,
  type TaskInbox,
  type TaskStats,
} from "@/lib/api-client";
import {
  EmptyState,
  PageBody,
  PageHeader,
  Section,
} from "@/components/design-system/page-shell";
import { EngineBadge } from "@/components/design-system/engine-badge";
import { SearchableClusterSelect } from "@/components/design-system/searchable-cluster-select";
import { fmtAgoKo, fmtClockKo, fmtExact, fmtRelative } from "@/lib/format";
import {
  advancePublishedMark,
  countNewPublications,
  isNewPublication,
  readPublishedMark,
} from "@/lib/tasks-watermark";
import { RcaReport } from "@/components/rca/rca-report";
import { categoryLabel } from "@/components/rca/rca-candidate-detail";
import { useT } from "@/lib/i18n";

const KIND_LABEL: Record<string, string> = {
  auto_rca: "자동 RCA",
  manual_rca: "수동 RCA",
  scheduled_report: "예약 리포트",
};

/** The ?kind values behind the scope control. Default is RCA only, because a
 *  recurring digest carries status "done" exactly like a report: measured, 100
 *  newest digest rows put ZERO reports on the page, and one hourly schedule
 *  fills 100 rows in about four days. The digests are one option away, not
 *  hidden. */
const KIND_SCOPE: { value: string; label: string; kind?: string }[] = [
  { value: "rca", label: "원인 분석만", kind: "auto_rca,manual_rca" },
  { value: "report", label: "예약 리포트만", kind: "scheduled_report" },
  { value: "all", label: "모든 종류" },
];

/** Extra pages `load` will follow when a page comes back EMPTY but carrying a
 *  cursor. The server walks a bounded number of index pages per request, so a
 *  caller whose rows all sit behind that budget gets an empty page and a resume
 *  key. Rendering that as an empty inbox states something the cursor already
 *  contradicts, so follow it instead of asking the operator to press 더 보기 to
 *  find out the page was not really empty. Bounded in turn: this runs on the
 *  poll too, and an unbounded chase over a 30-day table is a cost, not a
 *  feature. */
const AUTO_FOLLOW_PAGES = 4;

const STATUS_STYLE: Record<string, string> = {
  pending: "bg-zinc-700/30 text-zinc-300 border-zinc-600",
  running: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  done: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  failed: "bg-rose-500/15 text-rose-300 border-rose-500/40",
};
const STATUS_LABEL: Record<string, string> = {
  pending: "대기",
  running: "분석 중",
  done: "완료",
  failed: "실패",
};

function paramFromUrl(name: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(name) ?? "";
}

/** The row's one-line headline. The leading hypothesis when there is one, the
 *  actual state when there is not, and never "RCA completed": a row that says
 *  only that its task finished is the reason the operator had to open all
 *  eleven clusters. */
function rowHeadline(task: AgentTask, t: (ko: string) => string): string {
  if (task.finding?.summary) return task.finding.summary;
  if (task.status === "failed") return task.error || t("원인 미상 실패");
  if (task.status === "pending") return t("분석 대기 중");
  if (task.status === "running") return t("분석 중");
  if (task.summary) return task.summary;
  if (task.title) return task.title;
  return t("순위를 매길 후보를 찾지 못했습니다");
}

export default function TasksPage() {
  const t = useT();
  const [inbox, setInbox] = useState<TaskInbox | null>(null);
  const [stats, setStats] = useState<TaskStats | null>(null);
  const [engineByCluster, setEngineByCluster] = useState<
    Record<string, string>
  >({});
  // FLEET-WIDE by default. The only thing that narrows it on arrival is an
  // explicit ?cluster= in the link, and the scope control shows that it did.
  const [filterCluster, setFilterCluster] = useState<string>(() =>
    paramFromUrl("cluster"),
  );
  const [statusFilter, setStatusFilter] = useState<string>("");
  // RCA only by default: see KIND_SCOPE. Does NOT narrow the fleet publication
  // mark, which the server computes over every kind regardless of this.
  const [kindScope, setKindScope] = useState<string>("rca");
  // Pages 2+ of the history, appended by 더 보기. Page 1 lives in `inbox`.
  const [olderRows, setOlderRows] = useState<AgentTask[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(
    () => paramFromUrl("focus") || null,
  );
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<string | null>(null);

  // THE VISIT WATERMARK. Captured once and held fixed for the whole visit, so a
  // report that publishes while the operator is reading stays marked new until
  // the next visit. `undefined` means "not read yet" (localStorage does not
  // exist during the static export's prerender); `null` means first visit.
  const [visitMark, setVisitMark] = useState<string | null | undefined>(
    undefined,
  );
  useEffect(() => setVisitMark(readPublishedMark()), []);
  // Guards the one write: the FIRST successful unfiltered load of this visit.
  const advanced = useRef(false);

  const tasks = useMemo(() => {
    const seen = new Set<string>();
    const out: AgentTask[] = [];
    for (const x of [...(inbox?.tasks ?? []), ...olderRows]) {
      if (seen.has(x.task_id)) continue;
      seen.add(x.task_id);
      out.push(x);
    }
    return out;
  }, [inbox, olderRows]);

  const query = useMemo(
    () => ({
      cluster: filterCluster || undefined,
      status: statusFilter || undefined,
      kind: KIND_SCOPE.find((k) => k.value === kindScope)?.kind,
      limit: 100,
    }),
    [filterCluster, statusFilter, kindScope],
  );

  const load = useCallback(() => {
    const unfiltered = !filterCluster && !statusFilter;
    fetchTasks(query)
      .then(async (first) => {
        // An empty page carrying a cursor is the server saying "budget spent,
        // resume here", not "nothing to show". Follow it.
        let page = first;
        const followed: AgentTask[] = [];
        for (
          let i = 0;
          i < AUTO_FOLLOW_PAGES && page.tasks.length === 0 && page.next_cursor;
          i++
        ) {
          page = await fetchTasks({ ...query, cursor: page.next_cursor });
          followed.push(...page.tasks);
        }
        const rows = [...first.tasks, ...followed];
        const d: TaskInbox = {
          ...first,
          tasks: rows,
          count: rows.length,
          next_cursor: page.next_cursor,
        };
        setInbox(d);
        // Older pages belong to the page 1 they were fetched behind.
        setOlderRows([]);
        setErr(null);
        // Advance ONLY here: a successful load of the UNFILTERED inbox, once
        // per visit. Never on a poll (a report arriving while the operator
        // watches must stay new for the next visit), never on a failed load,
        // and never from a cluster- or status-filtered page, which carries no
        // fleet truth. The server computes the mark over the fleet recency
        // window under the caller's own tenancy, so it is never a max over
        // whatever rows this page happens to hold.
        if (unfiltered && !advanced.current) {
          advanced.current = true;
          advancePublishedMark(d.published_high_water_mark);
        }
      })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
    fetchTaskStats()
      .then(setStats)
      .catch(() => {});
  }, [filterCluster, statusFilter, query]);

  const loadMore = useCallback(() => {
    const cursor = inbox?.next_cursor;
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    fetchTasks({ ...query, cursor })
      .then((d) => {
        setOlderRows((prev) => [...prev, ...d.tasks]);
        // Only the cursor moves: page 1 and the visit mark stay put.
        setInbox((prev) =>
          prev ? { ...prev, next_cursor: d.next_cursor } : prev,
        );
      })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoadingMore(false));
  }, [inbox?.next_cursor, loadingMore, query]);

  useEffect(() => {
    load();
  }, [load]);

  // Engine map drives the per-row engine badge (the task row stores cluster_id,
  // not engine).
  useEffect(() => {
    fetchClusters()
      .then((rows: { cluster_id: string; engine?: string }[]) => {
        const m: Record<string, string> = {};
        for (const r of rows) if (r.engine) m[r.cluster_id] = r.engine;
        setEngineByCluster(m);
      })
      .catch(() => {});
  }, []);

  // Poll while anything is in flight so the user watches pending -> done live.
  const inFlight = useMemo(
    () => tasks.some((x) => x.status === "pending" || x.status === "running"),
    [tasks],
  );
  useEffect(() => {
    if (!inFlight) return;
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [inFlight, load]);

  const runManual = useCallback(async () => {
    if (!filterCluster) return;
    setRunning(true);
    setRunMsg(null);
    try {
      await createTask(filterCluster, "manual_rca");
      setRunMsg(
        t("RCA 작업을 시작했습니다. 잠시 후 아래에 결과가 나타납니다."),
      );
      load();
    } catch (e) {
      setRunMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }, [filterCluster, load]);

  const clusterOptions = useMemo(
    () =>
      Object.keys(engineByCluster).map((c) => ({
        cluster_id: c,
        engine: engineByCluster[c],
      })),
    [engineByCluster],
  );

  const newCount = countNewPublications(tasks, visitMark ?? null);
  // History remains behind this page and every row on it is new, so the count
  // is a floor. Say "이상" rather than quietly rounding down. Keyed on the
  // cursor, not on `count >= limit`: a page can be short and still have rows
  // behind it, so the row count never settles this.
  const capped =
    inbox !== null && inbox.next_cursor !== null && newCount === tasks.length;
  const countLabel =
    visitMark === undefined
      ? ""
      : visitMark === null
        ? t("첫 방문")
        : newCount === 0
          ? t("새 리포트 없음")
          : t(capped ? "{n}건 이상 신규" : "{n}건 신규").replace(
              "{n}",
              String(newCount),
            );

  return (
    <PageBody>
      <PageHeader
        eyebrow={t("automate")}
        title={t("RCA 받은함")}
        description={t(
          "클러스터를 하나씩 열지 않고 전체 RCA 리포트를 한 화면에서 읽습니다. 최근 도착한 리포트가 위에 오고, 마지막 방문 이후 도착한 리포트에는 표시가 붙습니다.",
        )}
      />
      <Section>
        {/* What arrived since the last visit, named honestly. This is not an
            unread count: it is "new since your last visit on this browser",
            and it never counts a queued, running or failed task because none of
            those has anything readable. */}
        {countLabel && (
          <div className="mb-4 flex flex-wrap items-baseline gap-x-3 gap-y-1 border border-zinc-800 bg-zinc-900/40 px-4 py-2.5">
            <span className="text-[10px] tracking-wider text-zinc-500 uppercase">
              {t("새 RCA 리포트")}
            </span>
            <span
              className={
                newCount > 0
                  ? "text-sm font-medium text-amber-300"
                  : "text-sm text-zinc-400"
              }
            >
              {countLabel}
            </span>
            <span className="text-[11px] text-zinc-500">
              {t(
                "이 브라우저에서 마지막 방문 이후 도착한 리포트 기준입니다. 읽음 여부가 아닙니다.",
              )}
            </span>
          </div>
        )}

        {stats && (
          <div className="mb-4 flex flex-wrap items-center gap-4 border border-zinc-800 bg-zinc-900/40 px-4 py-2.5 font-mono text-xs">
            <span className="text-zinc-400">
              {t("총 작업")}{" "}
              <span className="text-zinc-100">{fmtExact(stats.total)}</span>
            </span>
            <span className="text-zinc-400">
              {t("성공률")}{" "}
              <span className="text-emerald-300">
                {Math.round(stats.success_rate * 100)}%
              </span>
            </span>
            {stats.avg_duration_ms > 0 && (
              <span className="text-zinc-400">
                {t("평균 소요")}{" "}
                <span className="text-zinc-100">
                  {(stats.avg_duration_ms / 1000).toFixed(1)}s
                </span>
              </span>
            )}
            {Object.entries(stats.by_kind).map(([kind, count]) => (
              <span key={kind} className="text-zinc-500">
                {t(KIND_LABEL[kind] || kind)}{" "}
                <span className="text-zinc-300">{fmtExact(count)}</span>
              </span>
            ))}
            {stats.recent_failures > 0 && (
              <span className="text-rose-400">
                {t("최근 실패 {n}").replace(
                  "{n}",
                  fmtExact(stats.recent_failures),
                )}
              </span>
            )}
          </div>
        )}

        <div className="mb-4 flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-zinc-500">{t("범위")}</span>
          <SearchableClusterSelect
            value={filterCluster}
            onChange={setFilterCluster}
            clusters={clusterOptions}
            allowAll
            allLabel={t("모든 클러스터")}
            className="w-64"
          />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 focus:border-amber-500/60 focus:outline-none"
          >
            <option value="">{t("모든 상태")}</option>
            <option value="pending">{t("대기")}</option>
            <option value="running">{t("분석 중")}</option>
            <option value="done">{t("완료")}</option>
            <option value="failed">{t("실패")}</option>
          </select>
          <select
            value={kindScope}
            onChange={(e) => setKindScope(e.target.value)}
            className="border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 focus:border-amber-500/60 focus:outline-none"
            title={t(
              "예약 리포트는 완료 상태를 RCA와 공유하므로 상태 필터로는 분리되지 않습니다",
            )}
          >
            {KIND_SCOPE.map((k) => (
              <option key={k.value} value={k.value}>
                {t(k.label)}
              </option>
            ))}
          </select>
          <button
            onClick={load}
            className="border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 transition-colors hover:border-amber-500/40 hover:text-amber-300"
          >
            {t("새로고침")}
          </button>
          {filterCluster && (
            <>
              <button
                onClick={() => setFilterCluster("")}
                className="border border-zinc-700 px-3 py-1.5 text-xs text-zinc-400 transition-colors hover:border-zinc-500 hover:text-zinc-200"
              >
                {t("모든 클러스터 보기")}
              </button>
              <button
                onClick={runManual}
                disabled={running}
                className="border border-emerald-500/40 px-3 py-1.5 text-xs text-emerald-300 transition-colors hover:bg-emerald-500/10 disabled:opacity-50"
                title={t("{n}에 대해 RCA를 즉시 실행").replace(
                  "{n}",
                  filterCluster,
                )}
              >
                {running ? t("실행 중…") : t("▶ RCA 실행")}
              </button>
            </>
          )}
        </div>

        {/* The scope is never implicit: a filtered view says so, and says what
            it does NOT do to the fleet watermark. */}
        {filterCluster && (
          <div className="mb-3 border border-sky-500/30 bg-sky-500/5 px-3 py-2 text-[12px] text-sky-200/80">
            {t(
              "{n} 한 대로 범위를 좁혀 보고 있습니다. 신규 표시는 전체 받은함 기준이며, 이 화면은 방문 기록을 갱신하지 않습니다.",
            ).replace("{n}", filterCluster)}
          </div>
        )}
        {runMsg && <div className="mb-3 text-xs text-zinc-400">{runMsg}</div>}

        {err && (
          <div className="mb-3 text-sm text-rose-300">
            {t("조회 실패: {n}").replace("{n}", err)}
          </div>
        )}
        {loading ? (
          <div className="py-8 text-sm text-zinc-500">{t("불러오는 중…")}</div>
        ) : tasks.length === 0 ? (
          <EmptyState
            title={t("작업 없음")}
            description={
              kindScope === "all"
                ? t(
                    "경보가 발생하면 자동 RCA가 여기에 쌓입니다. 위에서 클러스터를 선택해 RCA를 직접 실행할 수도 있습니다.",
                  )
                : t(
                    "이 종류로는 아직 아무것도 없습니다. 범위를 모든 종류로 바꿔 보거나, 위에서 클러스터를 선택해 RCA를 직접 실행할 수 있습니다.",
                  )
            }
          />
        ) : (
          <div className="flex flex-col gap-2">
            {tasks.map((task) => (
              <TaskRow
                key={task.task_id}
                task={task}
                engine={engineByCluster[task.cluster_id]}
                isNew={isNewPublication(task.published_at, visitMark ?? null)}
                open={openId === task.task_id}
                onToggle={() =>
                  setOpenId(openId === task.task_id ? null : task.task_id)
                }
              />
            ))}
          </div>
        )}
        {/* Only the cursor may claim the history is exhausted. Without this the
            page silently drops the resume key and a report sitting behind a
            wall of digests is unreachable from the one place it is supposed to
            live. */}
        {!loading && inbox?.next_cursor && (
          <div className="mt-3 flex items-center gap-3">
            <button
              onClick={loadMore}
              disabled={loadingMore}
              className="border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 transition-colors hover:border-amber-500/40 hover:text-amber-300 disabled:opacity-50"
            >
              {loadingMore ? t("불러오는 중…") : t("더 보기")}
            </button>
            <span className="text-[11px] text-zinc-500">
              {t("{n}건 표시, 이전 기록이 더 있습니다").replace(
                "{n}",
                String(tasks.length),
              )}
            </span>
          </div>
        )}
      </Section>
      <SchedulesSection
        filterCluster={filterCluster}
        clusterOptions={clusterOptions}
      />
    </PageBody>
  );
}

const INTERVAL_LABEL: Record<string, string> = {
  hourly: "매시간",
  daily: "매일",
  weekly: "매주",
};

function SchedulesSection({
  filterCluster,
  clusterOptions,
}: {
  filterCluster: string;
  clusterOptions: { cluster_id: string; engine?: string }[];
}) {
  const t = useT();
  const [schedules, setSchedules] = useState<AgentSchedule[]>([]);
  const [intervalKind, setIntervalKind] = useState("daily");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchSchedules()
      .then((d) => setSchedules(d.schedules || []))
      .catch(() => {});
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const engineOf = useMemo(() => {
    const m: Record<string, string> = {};
    for (const c of clusterOptions) if (c.engine) m[c.cluster_id] = c.engine;
    return m;
  }, [clusterOptions]);

  const add = useCallback(async () => {
    if (!filterCluster) return;
    setBusy(true);
    setMsg(null);
    try {
      await createSchedule(filterCluster, intervalKind);
      load();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [filterCluster, intervalKind, load]);

  const remove = useCallback(
    async (id: number) => {
      try {
        await deleteSchedule(id);
        load();
      } catch (e) {
        setMsg(e instanceof Error ? e.message : String(e));
      }
    },
    [load],
  );

  return (
    <Section title={t("예약 작업")}>
      <p className="mb-3 text-xs text-zinc-500">
        {t(
          "반복 헬스 다이제스트를 예약합니다. 스케줄러가 주기마다 작업을 자동 등록하고, 결과는 위 목록과 토스트로 도착합니다.",
        )}
      </p>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-zinc-400">
          {filterCluster ||
            t("위 범위에서 클러스터를 선택하면 예약을 추가할 수 있습니다")}
        </span>
        {filterCluster && (
          <>
            <select
              value={intervalKind}
              onChange={(e) => setIntervalKind(e.target.value)}
              className="border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-200 focus:border-amber-500/60 focus:outline-none"
            >
              <option value="hourly">{t("매시간")}</option>
              <option value="daily">{t("매일")}</option>
              <option value="weekly">{t("매주")}</option>
            </select>
            <button
              onClick={add}
              disabled={busy}
              className="border border-emerald-500/40 px-3 py-1.5 text-xs text-emerald-300 transition-colors hover:bg-emerald-500/10 disabled:opacity-50"
            >
              {busy ? t("추가 중…") : t("+ 예약 추가")}
            </button>
          </>
        )}
      </div>
      {msg && <div className="mb-3 text-xs text-rose-300">{msg}</div>}
      {schedules.length === 0 ? (
        <div className="text-xs text-zinc-600">
          {t("등록된 예약이 없습니다.")}
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {schedules.map((s) => (
            <div
              key={s.id}
              className="flex items-center gap-3 border border-zinc-800 bg-zinc-900/40 px-4 py-2.5 text-sm"
            >
              <span className="flex-shrink-0 border border-sky-500/40 bg-sky-500/10 px-2 py-0.5 font-mono text-[10px] tracking-wider text-sky-300 uppercase">
                {t(INTERVAL_LABEL[s.interval_kind] || s.interval_kind)}
              </span>
              {engineOf[s.cluster_id] && (
                <EngineBadge
                  engine={engineOf[s.cluster_id]}
                  size="compact"
                  className="flex-shrink-0"
                />
              )}
              <span className="min-w-0 flex-1 truncate font-mono text-zinc-300">
                {s.cluster_id}
              </span>
              <span className="flex-shrink-0 font-mono text-[11px] text-zinc-500">
                {s.last_run_at
                  ? t("최근 {n}").replace("{n}", fmtRelative(s.last_run_at))
                  : t("미실행")}
              </span>
              <button
                onClick={() => remove(s.id)}
                className="flex-shrink-0 text-[11px] text-zinc-500 transition-colors hover:text-rose-300"
                title={t("예약 삭제")}
              >
                {t("삭제")}
              </button>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

function TaskRow({
  task,
  engine,
  isNew,
  open,
  onToggle,
}: {
  task: AgentTask;
  engine?: string;
  isNew: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const t = useT();
  const rowRef = useRef<HTMLDivElement>(null);
  // Deep-linked task (toast ?focus=) scrolls into view once on mount.
  useEffect(() => {
    if (open && rowRef.current) {
      rowRef.current.scrollIntoView({ block: "center" });
    }
    // Mount-only ON PURPOSE, so the dependency array stays empty: this scrolls
    // the row named by ?focus= into view once. Adding `open` would re-scroll on
    // every expand, which yanks the viewport whenever a user opens any row.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const expandable = task.status === "done" || task.status === "failed";
  const headline = rowHeadline(task, t);
  const category = task.finding?.category;

  return (
    <div
      ref={rowRef}
      // Stable hook for the real-input E2E, same convention as
      // data-app-header on the shell. Without it a spec can only select rows
      // by their Tailwind classes, which makes a restyle look like a
      // regression and left "더 보기 appends rather than replaces" unwritable.
      data-task-row={task.task_id}
      className={`border bg-zinc-900/40 transition-colors hover:border-zinc-700 ${
        isNew ? "border-amber-500/40" : "border-zinc-800"
      }`}
    >
      <button
        type="button"
        onClick={expandable ? onToggle : undefined}
        className={`flex w-full gap-3 px-4 py-3 text-left ${
          expandable ? "cursor-pointer" : "cursor-default"
        }`}
      >
        {/* The new indicator. A fixed-width gutter so every row's cluster name
            starts at the same x, new or not. */}
        <span className="w-2 flex-shrink-0 pt-2">
          {isNew && (
            <span
              className="block h-2 w-2 rounded-full bg-amber-400"
              title={t("마지막 방문 이후 도착한 리포트")}
            />
          )}
        </span>

        <span className="min-w-0 flex-1">
          {/* Cluster identity first and at reading size: this is WHOSE incident
              it is, not row metadata. */}
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[15px] font-medium text-zinc-100">
              {task.cluster_id}
            </span>
            {engine && (
              <EngineBadge
                engine={engine}
                size="compact"
                className="flex-shrink-0"
              />
            )}
            {isNew && (
              <span className="border border-amber-500/50 px-1.5 py-0.5 text-[10px] tracking-wider text-amber-300">
                {t("신규")}
              </span>
            )}
            <span
              className={`border px-1.5 py-0.5 font-mono text-[10px] tracking-wider uppercase ${
                STATUS_STYLE[task.status] || STATUS_STYLE.pending
              }`}
            >
              {t(STATUS_LABEL[task.status] || task.status)}
            </span>
          </span>

          {/* The finding headline: the leading hypothesis, at reading size. */}
          <span className="mt-1 block text-[14px] leading-relaxed text-zinc-200">
            {category && (
              <span className="text-zinc-500">
                {t("유력 가설")} ({t(categoryLabel(category))}):{" "}
              </span>
            )}
            {headline}
          </span>

          <span className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-zinc-500">
            <span>{t(KIND_LABEL[task.kind] || task.kind)}</span>
            <span className="font-mono">{task.trigger}</span>
            {/* The INCIDENT clock, kept visibly separate from the report clock
                on the right: an RCA published this morning about yesterday's
                event is new content about an old incident. */}
            {task.anchor_time && (
              <span>
                {t("인시던트 발생")} {fmtClockKo(task.anchor_time)}
              </span>
            )}
            {task.finding && task.finding.candidate_count > 1 && (
              <span>
                {t("후보 {n}건 중 1순위").replace(
                  "{n}",
                  String(task.finding.candidate_count),
                )}
              </span>
            )}
          </span>
        </span>

        <span className="flex flex-shrink-0 items-start gap-2 text-right">
          <span className="text-[11px] text-zinc-400">
            {task.published_at ? (
              <span title={fmtClockKo(task.published_at)}>
                {t("리포트 도착")} {fmtAgoKo(task.published_at)}
              </span>
            ) : (
              // No publication time exists yet (queued, running, failed) or the
              // row predates the stamp. The queue time is shown as the queue
              // time, never as an arrival.
              <span
                className="text-zinc-500"
                title={fmtClockKo(task.created_at)}
              >
                {t("등록 {n}").replace("{n}", fmtAgoKo(task.created_at))}
              </span>
            )}
          </span>
          {expandable && (
            <span className="text-xs text-zinc-600">{open ? "▾" : "▸"}</span>
          )}
        </span>
      </button>

      {open && task.status === "failed" && (
        <div className="border-t border-zinc-800/60 px-4 py-3 text-[13px] text-rose-300">
          {task.error || t("원인 미상 실패")}
        </div>
      )}
      {open && task.status === "done" && <RcaReport row={task} />}
    </div>
  );
}
