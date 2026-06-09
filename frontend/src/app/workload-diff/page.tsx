"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchWorkloadDiff, type WorkloadDiffResponse } from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  EmptyState,
} from "@/components/design-system/page-shell";
import { useSelectedCluster } from "@/lib/use-selected-cluster";
import { ClusterPicker } from "@/components/design-system/cluster-picker";

// Default to "24h ago → now" — the most common "what changed since
// yesterday's deploy" framing. datetime-local needs no timezone suffix.
function isoLocal(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

export default function WorkloadDiffPage() {
  // Global cluster selection (shared store) — stays in sync with ⌘K / header.
  const { selected: clusterId } = useSelectedCluster();
  const now = new Date();
  const dayAgo = new Date(now.getTime() - 24 * 3600 * 1000);
  const [before, setBefore] = useState(isoLocal(dayAgo));
  const [after, setAfter] = useState(isoLocal(now));
  const [regressionPct, setRegressionPct] = useState(20);
  const [data, setData] = useState<WorkloadDiffResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Timeline deep-link: ?center=ISO sets `before` to that event time (the
  // canonical "what changed since this event"). Cluster itself comes from the
  // shared store (?cluster= is honored there too).
  useEffect(() => {
    const center = new URLSearchParams(window.location.search).get("center");
    if (center) {
      const c = new Date(center);
      if (!Number.isNaN(c.getTime())) setBefore(isoLocal(c));
    }
  }, []);

  const run = useCallback(() => {
    if (!clusterId) return;
    setLoading(true);
    setError(null);
    // datetime-local has no zone; treat as local and convert to ISO.
    const beforeIso = new Date(before).toISOString();
    const afterIso = new Date(after).toISOString();
    fetchWorkloadDiff(clusterId, beforeIso, afterIso, { regressionPct })
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [clusterId, before, after, regressionPct]);

  return (
    <PageBody>
      <PageHeader
        eyebrow="incident"
        title="Workload diff"
        description="두 시점의 쿼리 워크로드(pg_stat_statements)를 비교 — 배포 이후 새로 등장한 쿼리, 갑자기 느려진 쿼리, 사라진 쿼리를 자동 검출. '배포하고 느려졌다'는 신고에 30초 안에 용의자를 좁힙니다."
        actions={<ClusterPicker selected={clusterId} />}
      />

      {/* Controls */}
      <div className="border border-zinc-800 bg-zinc-900/40 p-4 mb-6 flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wider text-zinc-500">
            Before (기준 시점)
          </span>
          <input
            type="datetime-local"
            value={before}
            onChange={(e) => setBefore(e.target.value)}
            className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 focus:outline-none focus:border-amber-500/60"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wider text-zinc-500">
            After (비교 시점)
          </span>
          <input
            type="datetime-local"
            value={after}
            onChange={(e) => setAfter(e.target.value)}
            className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 focus:outline-none focus:border-amber-500/60"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wider text-zinc-500">
            Regression 임계 (%)
          </span>
          <input
            type="number"
            min={5}
            step={5}
            value={regressionPct}
            onChange={(e) => setRegressionPct(Number(e.target.value))}
            className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 w-24 font-mono focus:outline-none focus:border-amber-500/60"
          />
        </label>
        <button
          onClick={run}
          disabled={loading}
          className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors"
        >
          {loading ? "비교 중…" : "비교 실행"}
        </button>
      </div>

      {error && (
        <div className="mb-4 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {error}
        </div>
      )}

      {data && (
        <>
          {/* Summary row */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-zinc-800 border border-zinc-800 mb-6">
            <SummaryCell
              label="🆕 New"
              value={data.totals.new}
              accent="amber"
            />
            <SummaryCell
              label="📈 Regressed"
              value={data.totals.regressed}
              accent="rose"
            />
            <SummaryCell
              label="📉 Improved"
              value={data.totals.improved}
              accent="emerald"
            />
            <SummaryCell
              label="👻 Disappeared"
              value={data.totals.disappeared}
              accent="zinc"
            />
          </div>

          <NewBlock rows={data.new} />
          <RegressedBlock
            title="📈 Regressed queries"
            rows={data.regressed}
            worse
          />
          <RegressedBlock title="📉 Improved queries" rows={data.improved} />
          <DisappearedBlock rows={data.disappeared} />

          <p className="text-[11px] text-zinc-600 mt-6 leading-relaxed border-l-2 border-zinc-800 pl-3">
            {data.methodology}
          </p>
        </>
      )}

      {!data && !loading && (
        <EmptyState
          eyebrow="workload diff"
          title="두 시점을 골라 비교를 실행하세요"
          description="기본값은 '24시간 전 → 지금'. 배포 직전 시각을 Before에 넣으면 그 배포가 워크로드에 무엇을 했는지 바로 보입니다."
        />
      )}
    </PageBody>
  );
}

function SummaryCell({
  label,
  value,
  accent,
}: {
  label: string;
  value: number;
  accent: "amber" | "rose" | "emerald" | "zinc";
}) {
  const color = {
    amber: "text-amber-300",
    rose: "text-rose-300",
    emerald: "text-emerald-300",
    zinc: "text-zinc-400",
  }[accent];
  return (
    <div className="bg-zinc-950 px-4 py-4">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
        {label}
      </div>
      <div className={`text-2xl font-semibold tabular-nums ${color}`}>
        {value}
      </div>
    </div>
  );
}

function QueryExcerpt({ text }: { text: string }) {
  return (
    <pre className="text-[11px] text-zinc-300 font-mono whitespace-pre-wrap break-all">
      {(text || "").trim()}
    </pre>
  );
}

function NewBlock({ rows }: { rows: WorkloadDiffResponse["new"] }) {
  if (!rows.length) return null;
  return (
    <section className="mb-6">
      <div className="text-[11px] font-medium text-zinc-500 mb-2">
        🆕 New queries — before엔 없던 쿼리
      </div>
      <div className="border border-zinc-800 divide-y divide-zinc-800">
        {rows.map((r) => (
          <div key={r.query_hash} className="px-4 py-3">
            <div className="flex items-baseline justify-between gap-3 mb-1">
              <span className="text-[10px] font-mono text-zinc-500">
                {r.query_hash.slice(0, 12)}…
              </span>
              <span className="text-[11px] text-zinc-400 tabular-nums">
                mean {Math.round(r.mean_time_ms)}ms · {r.calls} calls
              </span>
            </div>
            <QueryExcerpt text={r.query_excerpt} />
          </div>
        ))}
      </div>
    </section>
  );
}

function RegressedBlock({
  title,
  rows,
  worse,
}: {
  title: string;
  rows: WorkloadDiffResponse["regressed"];
  worse?: boolean;
}) {
  if (!rows.length) return null;
  return (
    <section className="mb-6">
      <div className="text-[11px] font-medium text-zinc-500 mb-2">{title}</div>
      <div className="border border-zinc-800 divide-y divide-zinc-800">
        {rows.map((r) => (
          <div key={r.query_hash} className="px-4 py-3">
            <div className="flex items-baseline justify-between gap-3 mb-1">
              <span className="text-[10px] font-mono text-zinc-500">
                {r.query_hash.slice(0, 12)}…
              </span>
              <span className="text-[11px] tabular-nums">
                <span className="text-zinc-500">
                  {r.before_mean_ms}ms → {r.after_mean_ms}ms
                </span>
                <span
                  className={`ml-2 font-medium ${
                    worse ? "text-rose-300" : "text-emerald-300"
                  }`}
                >
                  {r.delta_pct > 0 ? "+" : ""}
                  {r.delta_pct}%
                </span>
              </span>
            </div>
            <QueryExcerpt text={r.query_excerpt} />
          </div>
        ))}
      </div>
    </section>
  );
}

function DisappearedBlock({
  rows,
}: {
  rows: WorkloadDiffResponse["disappeared"];
}) {
  if (!rows.length) return null;
  return (
    <section className="mb-6">
      <div className="text-[11px] font-medium text-zinc-500 mb-2">
        👻 Disappeared — after엔 사라진 쿼리 (참고용)
      </div>
      <div className="border border-zinc-800 divide-y divide-zinc-800">
        {rows.map((r) => (
          <div key={r.query_hash} className="px-4 py-2.5">
            <div className="flex items-baseline justify-between gap-3 mb-1">
              <span className="text-[10px] font-mono text-zinc-600">
                {r.query_hash.slice(0, 12)}…
              </span>
              <span className="text-[11px] text-zinc-600 tabular-nums">
                was {Math.round(r.mean_time_ms)}ms
              </span>
            </div>
            <QueryExcerpt text={r.query_excerpt} />
          </div>
        ))}
      </div>
    </section>
  );
}
