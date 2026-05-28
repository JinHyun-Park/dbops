"use client";

import { useEffect, useState } from "react";
import {
  fetchRedundantIndexes,
  type RedundantIndexCandidate,
  type RedundantIndexesResponse,
  type RedundantIndexKind,
} from "@/lib/api-client";
import { fmtBytes, fmtExact } from "@/lib/format";

// Tiny chip — same visual language as the rest of the dashboard.
const KIND_STYLES: Record<
  RedundantIndexKind,
  { label: string; classes: string; hint: string }
> = {
  prefix: {
    label: "prefix",
    classes: "bg-amber-500/10 text-amber-300 border-amber-500/40",
    hint: "이 인덱스 컬럼이 다른 인덱스의 앞부분에 그대로 포함됩니다. 더 긴 인덱스가 같은 쿼리를 처리할 수 있어 드롭 후보.",
  },
  duplicate: {
    label: "duplicate",
    classes: "bg-rose-500/10 text-rose-300 border-rose-500/40",
    hint: "다른 인덱스와 컬럼 구성이 완전히 같습니다. 마이그레이션 잔여물일 가능성 — 작은 쪽을 드롭.",
  },
  unused: {
    label: "unused",
    classes: "bg-zinc-700/40 text-zinc-300 border-zinc-700",
    hint: "통계 리셋 이후 idx_scan = 0. unique/PK 제약을 받쳐주지 않으면 드롭 검토 대상.",
  },
};

export function RedundantIndexesPanel({ clusterId }: { clusterId: string }) {
  const [data, setData] = useState<RedundantIndexesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Don't auto-load on mount — this hits the live cluster via Data API and
  // can be slow on big schemas. Manual button matches Log Insights.

  const load = async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await fetchRedundantIndexes(clusterId);
      setData(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  };

  // Reset when cluster changes — the old cluster's index list isn't useful.
  useEffect(() => {
    setData(null);
    setErr(null);
  }, [clusterId]);

  const candidates = data?.candidates ?? [];
  const reclaimable = data?.total_bytes_reclaimable ?? 0;

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <div className="text-sm text-zinc-200 font-medium">
            Redundant Indexes
            {data && data.candidates_count != null && (
              <span className="ml-2 px-1.5 py-0.5 bg-rose-500/15 text-rose-300 border border-rose-500/40 text-[10px]">
                {data.candidates_count}
              </span>
            )}
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            prefix-covered / 완전 중복 / unused 인덱스를 라이브 클러스터에서
            검출. PostgreSQL 전용.
          </div>
        </div>
        <div className="flex items-center gap-2">
          {data && data.indexes_scanned != null && (
            <span className="text-[10px] text-zinc-600 font-mono">
              {fmtExact(data.indexes_scanned)}개 인덱스 스캔
            </span>
          )}
          <button
            onClick={load}
            disabled={loading}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors"
          >
            {loading ? "검색 중…" : data ? "새로고침" : "검출 실행"}
          </button>
        </div>
      </div>

      <div className="max-h-96 overflow-y-auto">
        {!data && !loading && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            <span className="text-amber-300">검출 실행</span> 버튼을 누르면
            라이브 클러스터의 pg_index를 한 번 조회해서 후보를 뽑아냅니다.
            인덱스가 많을수록 1~3초 정도 걸립니다.
          </div>
        )}
        {loading && (
          <div className="p-6 text-zinc-500 text-sm">불러오는 중…</div>
        )}
        {err && (
          <div className="p-5">
            <div className="text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
              {err}
            </div>
          </div>
        )}
        {data?.info && (
          <div className="p-5">
            <div className="text-xs text-zinc-300 border border-zinc-700 bg-zinc-900/40 px-3 py-2">
              {data.info}
            </div>
          </div>
        )}
        {data?.error && !data?.info && (
          <div className="p-5">
            <div className="text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
              {data.error}
              {data.message && (
                <div className="mt-1 text-[11px] text-zinc-400 font-mono">
                  {data.message}
                </div>
              )}
            </div>
          </div>
        )}
        {data && !data.error && candidates.length === 0 && (
          <div className="p-6 text-emerald-400 text-sm flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-emerald-500" />
            검출된 중복/미사용 인덱스 없음 — 인덱스 구성 양호 🎉
          </div>
        )}
        {candidates.length > 0 && (
          <>
            <div className="px-4 py-2 border-b border-zinc-800 bg-zinc-900/40 text-[11px] text-zinc-400">
              회수 가능 디스크 ≈{" "}
              <span className="text-zinc-200 font-mono">
                {fmtBytes(reclaimable)}
              </span>{" "}
              · 드롭 전에 항상 <code className="text-amber-300">EXPLAIN</code>
              으로 실제 쿼리 영향 검증 권장
            </div>
            <table className="w-full text-sm">
              <thead className="bg-zinc-900/60 border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-500 sticky top-0">
                <tr>
                  <th className="text-left px-3 py-2 font-medium">테이블</th>
                  <th className="text-left px-3 py-2 font-medium">인덱스</th>
                  <th className="text-left px-3 py-2 font-medium w-24">Kind</th>
                  <th className="text-right px-3 py-2 font-medium w-20">
                    크기
                  </th>
                  <th
                    className="text-right px-3 py-2 font-medium w-20"
                    title="통계 리셋 이후 scan 횟수"
                  >
                    Scans
                  </th>
                  <th className="text-left px-3 py-2 font-medium">
                    Covered by / Columns
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800">
                {candidates.map((c) => (
                  <tr key={`${c.schema}.${c.table}.${c.index_name}`}>
                    <td className="px-3 py-2 font-mono text-[11px] text-zinc-300 align-top">
                      <span className="text-zinc-500">{c.schema}.</span>
                      {c.table}
                    </td>
                    <td
                      className="px-3 py-2 font-mono text-[11px] text-zinc-200 align-top break-all"
                      title={c.definition}
                    >
                      {c.index_name}
                    </td>
                    <td className="px-3 py-2 align-top">
                      <KindBadge candidate={c} />
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-[11px] text-zinc-300 align-top tabular-nums">
                      {fmtBytes(c.bytes)}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-[11px] text-zinc-400 align-top tabular-nums">
                      {fmtExact(c.idx_scan)}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-zinc-400 align-top break-all">
                      {c.covered_by ? (
                        <>
                          <span className="text-zinc-500">→ </span>
                          <span className="text-zinc-200">{c.covered_by}</span>
                        </>
                      ) : (
                        <span className="text-zinc-500">{c.columns}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}

function KindBadge({ candidate }: { candidate: RedundantIndexCandidate }) {
  const k = KIND_STYLES[candidate.kind];
  return (
    <span
      className={`px-1.5 py-0.5 border text-[10px] font-mono ${k.classes}`}
      title={k.hint}
    >
      {k.label}
    </span>
  );
}
