"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createSchedule,
  createTask,
  deleteSchedule,
  fetchClusters,
  fetchSchedules,
  fetchTasks,
  type AgentSchedule,
  type AgentTask,
} from "@/lib/api-client";
import {
  EmptyState,
  PageBody,
  PageHeader,
  Section,
} from "@/components/design-system/page-shell";
import { EngineBadge } from "@/components/design-system/engine-badge";
import { SearchableClusterSelect } from "@/components/design-system/searchable-cluster-select";
import { getSelectedCluster } from "@/lib/selected-cluster";
import { fmtRelative } from "@/lib/format";

const KIND_LABEL: Record<string, string> = {
  auto_rca: "자동 RCA",
  manual_rca: "수동 RCA",
  scheduled_report: "예약 리포트",
};

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

function focusFromUrl(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("focus");
}

function isoFromMs(ms: string | undefined): string | undefined {
  if (!ms) return undefined;
  const n = Number(ms);
  return Number.isFinite(n) ? new Date(n).toISOString() : undefined;
}

export default function TasksPage() {
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [engineByCluster, setEngineByCluster] = useState<
    Record<string, string>
  >({});
  const [filterCluster, setFilterCluster] = useState<string>(
    () => getSelectedCluster() ?? "",
  );
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(() => focusFromUrl());
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchTasks({
      cluster: filterCluster || undefined,
      status: statusFilter || undefined,
      limit: 100,
    })
      .then((d) => {
        setTasks(d.tasks || []);
        setErr(null);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [filterCluster, statusFilter]);

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
    () => tasks.some((t) => t.status === "pending" || t.status === "running"),
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
      setRunMsg("RCA 작업을 시작했습니다 — 잠시 후 아래에 결과가 나타납니다.");
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

  return (
    <PageBody>
      <PageHeader
        eyebrow="automate"
        title="에이전트 작업"
        description="경보 자동 RCA · 예약 · 수동 실행 작업의 기록과 결과. 모든 작업은 읽기 전용 분석입니다 — 변경은 승인 센터를 거칩니다."
      />
      <Section>
        <div className="flex items-center gap-2 flex-wrap mb-4">
          <SearchableClusterSelect
            value={filterCluster}
            onChange={setFilterCluster}
            clusters={clusterOptions}
            allowAll
            allLabel="모든 클러스터"
            className="w-64"
          />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-sm px-3 py-1.5 focus:outline-none focus:border-amber-500/60"
          >
            <option value="">모든 상태</option>
            <option value="pending">대기</option>
            <option value="running">분석 중</option>
            <option value="done">완료</option>
            <option value="failed">실패</option>
          </select>
          <button
            onClick={load}
            className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:text-amber-300 hover:border-amber-500/40 transition-colors"
          >
            새로고침
          </button>
          {filterCluster && (
            <button
              onClick={runManual}
              disabled={running}
              className="text-xs px-3 py-1.5 border border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10 transition-colors disabled:opacity-50"
              title={`${filterCluster}에 대해 RCA를 즉시 실행`}
            >
              {running ? "실행 중…" : "▶ RCA 실행"}
            </button>
          )}
        </div>
        {runMsg && <div className="text-xs text-zinc-400 mb-3">{runMsg}</div>}

        {err && (
          <div className="text-rose-300 text-sm mb-3">조회 실패: {err}</div>
        )}
        {loading ? (
          <div className="text-zinc-500 text-sm py-8">불러오는 중…</div>
        ) : tasks.length === 0 ? (
          <EmptyState
            title="작업 없음"
            description="경보가 발생하면 자동 RCA가 여기에 쌓입니다. 위에서 클러스터를 선택해 RCA를 직접 실행할 수도 있습니다."
          />
        ) : (
          <div className="flex flex-col gap-2">
            {tasks.map((t) => (
              <TaskRow
                key={t.task_id}
                task={t}
                engine={engineByCluster[t.cluster_id]}
                open={openId === t.task_id}
                onToggle={() =>
                  setOpenId(openId === t.task_id ? null : t.task_id)
                }
              />
            ))}
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
    <Section title="예약 작업">
      <p className="text-xs text-zinc-500 mb-3">
        반복 헬스 다이제스트를 예약합니다. 스케줄러가 주기마다 작업을 자동
        등록하고, 결과는 위 목록과 토스트로 도착합니다.
      </p>
      <div className="flex items-center gap-2 flex-wrap mb-4">
        <span className="text-xs text-zinc-400 font-mono">
          {filterCluster ||
            "위에서 클러스터를 선택하면 예약을 추가할 수 있습니다"}
        </span>
        {filterCluster && (
          <>
            <select
              value={intervalKind}
              onChange={(e) => setIntervalKind(e.target.value)}
              className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-sm px-3 py-1.5 focus:outline-none focus:border-amber-500/60"
            >
              <option value="hourly">매시간</option>
              <option value="daily">매일</option>
              <option value="weekly">매주</option>
            </select>
            <button
              onClick={add}
              disabled={busy}
              className="text-xs px-3 py-1.5 border border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10 transition-colors disabled:opacity-50"
            >
              {busy ? "추가 중…" : "+ 예약 추가"}
            </button>
          </>
        )}
      </div>
      {msg && <div className="text-xs text-rose-300 mb-3">{msg}</div>}
      {schedules.length === 0 ? (
        <div className="text-xs text-zinc-600">등록된 예약이 없습니다.</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {schedules.map((s) => (
            <div
              key={s.id}
              className="flex items-center gap-3 border border-zinc-800 bg-zinc-900/40 px-4 py-2.5 text-sm"
            >
              <span className="flex-shrink-0 px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-sky-500/40 bg-sky-500/10 text-sky-300">
                {INTERVAL_LABEL[s.interval_kind] || s.interval_kind}
              </span>
              {engineOf[s.cluster_id] && (
                <EngineBadge
                  engine={engineOf[s.cluster_id]}
                  size="compact"
                  className="flex-shrink-0"
                />
              )}
              <span className="flex-1 min-w-0 truncate font-mono text-zinc-300">
                {s.cluster_id}
              </span>
              <span className="flex-shrink-0 text-[11px] text-zinc-500 font-mono">
                {s.last_run_at
                  ? `최근 ${fmtRelative(s.last_run_at)}`
                  : "미실행"}
              </span>
              <button
                onClick={() => remove(s.id)}
                className="flex-shrink-0 text-[11px] text-zinc-500 hover:text-rose-300 transition-colors"
                title="예약 삭제"
              >
                삭제
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
  open,
  onToggle,
}: {
  task: AgentTask;
  engine?: string;
  open: boolean;
  onToggle: () => void;
}) {
  const rowRef = useRef<HTMLDivElement>(null);
  // Deep-linked task (toast ?focus=) scrolls into view once on mount.
  useEffect(() => {
    if (open && rowRef.current) {
      rowRef.current.scrollIntoView({ block: "center" });
    }
    // eslint-disable-line react-hooks/exhaustive-deps
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const candidates = task.result?.candidates ?? [];
  // scheduled_report results carry normalized display lines instead of RCA
  // candidates — render them as a key/value digest.
  const reportLines =
    (task.result as { lines?: { label: string; value: string }[] } | undefined)
      ?.lines ?? null;
  const expandable = task.status === "done" || task.status === "failed";

  return (
    <div
      ref={rowRef}
      className="border border-zinc-800 bg-zinc-900/40 hover:border-zinc-700 transition-colors"
    >
      <button
        type="button"
        onClick={expandable ? onToggle : undefined}
        className={`w-full flex items-center gap-3 px-4 py-3 text-left ${
          expandable ? "cursor-pointer" : "cursor-default"
        }`}
      >
        <span
          className={`flex-shrink-0 px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border ${
            STATUS_STYLE[task.status] || STATUS_STYLE.pending
          }`}
        >
          {STATUS_LABEL[task.status] || task.status}
        </span>
        <span className="flex-shrink-0 text-[11px] font-mono text-zinc-400 w-20">
          {KIND_LABEL[task.kind] || task.kind}
        </span>
        {engine && (
          <EngineBadge
            engine={engine}
            size="compact"
            className="flex-shrink-0"
          />
        )}
        <span className="flex-1 min-w-0 truncate text-sm text-zinc-200">
          <span className="font-mono text-zinc-400">{task.cluster_id}</span>
          {task.summary && (
            <span className="text-zinc-300"> — {task.summary}</span>
          )}
        </span>
        <span className="flex-shrink-0 text-[11px] text-zinc-500 font-mono">
          {fmtRelative(isoFromMs(task.created_at))}
        </span>
        {expandable && (
          <span className="flex-shrink-0 text-zinc-600 text-xs">
            {open ? "▾" : "▸"}
          </span>
        )}
      </button>

      {open && task.status === "failed" && (
        <div className="px-4 pb-3 text-xs text-rose-300 font-mono">
          {task.error || "원인 미상 실패"}
        </div>
      )}

      {open && task.status === "done" && (
        <div className="px-4 pb-4 border-t border-zinc-800/60 pt-3">
          {reportLines ? (
            <dl className="grid grid-cols-2 gap-x-8 gap-y-1">
              {reportLines.map((l, i) => (
                <div
                  key={i}
                  className="flex items-center justify-between gap-3 text-xs"
                >
                  <dt className="text-zinc-500 font-mono">{l.label}</dt>
                  <dd className="text-zinc-200 font-mono">{l.value}</dd>
                </div>
              ))}
            </dl>
          ) : candidates.length === 0 ? (
            <div className="text-xs text-zinc-500">
              자동 수집 신호에서 뚜렷한 원인을 찾지 못했습니다. 수동 점검을
              권장합니다.
            </div>
          ) : (
            <ol className="flex flex-col gap-2">
              {candidates.map((c, i) => (
                <li
                  key={c.rank ?? i}
                  className="flex items-start gap-3 text-sm"
                >
                  <span className="flex-shrink-0 w-5 text-zinc-500 font-mono">
                    #{c.rank ?? i + 1}
                  </span>
                  <span className="flex-shrink-0 text-[10px] font-mono uppercase tracking-wider px-1.5 py-0.5 border border-zinc-700 text-zinc-400 mt-0.5">
                    {String(c.category ?? "—")}
                  </span>
                  <span className="flex-1 min-w-0">
                    <span className="text-zinc-200">
                      {String(c.summary ?? "")}
                    </span>
                    <span className="block text-[11px] text-zinc-500 font-mono mt-0.5">
                      score {String(c.score ?? "—")}
                      {c.when ? ` · ${String(c.when)}` : ""}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}
          {task.result?.note && (
            <div className="text-[11px] text-zinc-600 mt-3 italic">
              {String(task.result.note)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
