"use client";

import { useEffect, useState } from "react";
import { fetchLearning, type AggRow, type RecentCase } from "@/lib/api-client";
import { confidence, trackRecordLabel } from "@/lib/remediation";
import { fmtExact } from "@/lib/format";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color =
    pct >= 70 ? "bg-emerald-500" : pct >= 40 ? "bg-amber-500" : "bg-slate-600";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-800">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-[11px] text-slate-500">{pct}%</span>
    </div>
  );
}

function AggTable({ rows }: { rows: AggRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="overflow-hidden rounded-lg border border-slate-800">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-800 bg-slate-900/60">
            <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-slate-500">
              Symptom class
            </th>
            <th className="px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-slate-500">
              Action class
            </th>
            <th className="px-3 py-2 text-right text-[11px] font-medium uppercase tracking-wider text-slate-500">
              이력
            </th>
            <th className="px-3 py-2 text-right text-[11px] font-medium uppercase tracking-wider text-slate-500">
              신뢰도 (Wilson)
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/60">
          {rows.map((r, i) => (
            <tr
              key={`${r.cluster_id ?? "fleet"}:${r.symptom_class}:${
                r.action_class
              }:${i}`}
              className="hover:bg-slate-900/40"
            >
              <td className="px-3 py-2.5 font-mono text-xs text-slate-300">
                {r.symptom_class}
              </td>
              <td className="px-3 py-2.5 font-mono text-xs text-slate-400">
                {r.action_class}
              </td>
              <td className="px-3 py-2.5 text-right text-xs text-slate-400">
                {trackRecordLabel(r.successes, r.attempts)}
                {r.attempts >= 1000 && (
                  <span className="ml-1 text-slate-600">
                    ({fmtExact(r.attempts)})
                  </span>
                )}
              </td>
              <td className="px-3 py-2.5">
                <div className="flex justify-end">
                  <ConfidenceBar value={confidence(r.successes, r.attempts)} />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const STATUS_CHIP: Record<RecentCase["status"], string> = {
  success: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  failure: "bg-rose-500/15 text-rose-400 border-rose-500/30",
  pending: "bg-amber-500/15 text-amber-400 border-amber-500/30",
};
const STATUS_LABEL: Record<RecentCase["status"], string> = {
  success: "해결",
  failure: "미해결",
  pending: "평가 대기",
};

export default function LearningPage() {
  const [data, setData] = useState<Awaited<
    ReturnType<typeof fetchLearning>
  > | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetchLearning()
      .then(setData)
      .catch((e) => setErr(String(e)));
  }, []);

  const clusterEntries = data ? Object.entries(data.clusters) : [];
  const isEmpty =
    data &&
    data.fleet.length === 0 &&
    clusterEntries.length === 0 &&
    data.recent.length === 0;

  return (
    <>
      <PageHeader
        eyebrow="Monitor"
        title="Learning"
        description="권장 조치가 실제로 증상을 해소했는지 자동 측정해 누적한 효과 이력 — 입증된 조치를 우선합니다."
      />
      <PageBody>
        {err ? (
          <EmptyState title="불러오지 못했습니다" description={err} />
        ) : !data ? (
          <div className="py-16 text-center text-sm text-slate-500">
            불러오는 중…
          </div>
        ) : isEmpty ? (
          <EmptyState
            title="아직 학습된 결과가 없습니다"
            description="권장 조치가 적용되고 평가 윈도우가 지나면 효과 이력이 쌓입니다."
          />
        ) : (
          <div className="space-y-8">
            {/* Fleet-wide aggregates */}
            {data.fleet.length > 0 && (
              <section>
                <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-500">
                  Fleet 전체 — 조치별 효과
                </h2>
                <AggTable rows={data.fleet} />
              </section>
            )}

            {/* Per-cluster aggregates */}
            {clusterEntries.length > 0 && (
              <section>
                <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-500">
                  클러스터별
                </h2>
                <div className="space-y-4">
                  {clusterEntries.map(([clusterId, rows]) => (
                    <div key={clusterId}>
                      <div className="mb-2 font-mono text-xs font-medium text-slate-400">
                        {clusterId}
                      </div>
                      <AggTable rows={rows} />
                    </div>
                  ))}
                </div>
              </section>
            )}

            {/* Recent outcomes */}
            {data.recent.length > 0 && (
              <section>
                <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-500">
                  최근 평가 결과
                </h2>
                <div className="space-y-1.5">
                  {data.recent.map((c, i) => (
                    <div
                      key={`${c.cluster_id}:${c.evaluated_at}:${i}`}
                      className="flex flex-wrap items-center gap-3 rounded-lg border border-slate-800 bg-slate-900/30 px-3 py-2.5 text-xs"
                    >
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${
                          STATUS_CHIP[c.status]
                        }`}
                      >
                        {STATUS_LABEL[c.status]}
                      </span>
                      <span className="font-mono text-slate-300">
                        {c.cluster_id}
                      </span>
                      <span className="text-slate-500">
                        {c.symptom_class} → {c.action_class}
                      </span>
                      <span className="ml-auto text-slate-600">
                        {new Date(c.evaluated_at).toLocaleString("ko-KR", {
                          month: "2-digit",
                          day: "2-digit",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}
          </div>
        )}
      </PageBody>
    </>
  );
}
