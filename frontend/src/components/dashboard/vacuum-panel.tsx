"use client";

import { useEffect, useState } from "react";
import { fetchVacuumStats, fetchHealthFindings } from "@/lib/api-client";
import { fmtExact, fmtNumber } from "@/lib/format";

interface Table {
  schema_name: string;
  table_name: string;
  n_live_tup: number | string;
  n_dead_tup: number | string;
  bloat_ratio: number | string;
  seq_scan: number | string;
  idx_scan: number | string;
  last_vacuum: string | null;
  last_analyze: string | null;
}

// 200M transactions = "warn ahead of wraparound", 1.5B = "fix immediately".
// pg_health_checks emits findings above these; we color cells the same way.
const TXID_WARN = 200_000_000;
const TXID_CRITICAL = 1_500_000_000;

function n(v: unknown) {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

function safeJSON(s: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(s);
    return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function relDays(iso: string | null) {
  if (!iso) return "없음";
  const ms = Date.now() - new Date(iso).getTime();
  const d = Math.floor(ms / 86400000);
  if (d < 1) {
    const h = Math.floor(ms / 3600000);
    return h < 1 ? "<1시간" : `${h}시간 전`;
  }
  return `${d}일 전`;
}

export function VacuumPanel({ clusterId }: { clusterId: string }) {
  const [tables, setTables] = useState<Table[]>([]);
  const [txidAgeByTable, setTxidAgeByTable] = useState<Record<string, number>>(
    {},
  );
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [stats, findings] = await Promise.allSettled([
          fetchVacuumStats(clusterId),
          fetchHealthFindings(clusterId),
        ]);
        if (cancelled) return;
        if (stats.status === "fulfilled") {
          setTables(stats.value.tables || []);
        } else {
          setTables([]);
        }
        if (findings.status === "fulfilled") {
          const ageMap: Record<string, number> = {};
          for (const f of findings.value.findings || []) {
            if (f.check_type !== "txid_age") continue;
            const d =
              typeof f.details === "string"
                ? safeJSON(f.details)
                : (f.details as Record<string, unknown> | null) || null;
            const schema = d && typeof d.schema === "string" ? d.schema : null;
            const tbl = d && typeof d.table === "string" ? d.table : null;
            const age = d && typeof d.age === "number" ? d.age : null;
            if (schema && tbl && age != null) ageMap[`${schema}.${tbl}`] = age;
          }
          setTxidAgeByTable(ageMap);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    const iv = setInterval(load, 60000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId]);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800">
        <div className="text-sm text-zinc-200 font-medium">Vacuum & Bloat</div>
        <div className="text-[11px] text-zinc-500 mt-0.5">
          bloat 비율(dead / 전체 튜플) 내림차순 정렬
        </div>
      </div>
      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">불러오는 중…</div>
      ) : tables.length === 0 ? (
        <div className="p-6 text-zinc-500 text-sm">
          테이블 통계 없음 (PG 전용 · 5분 주기 수집)
        </div>
      ) : (
        <div className="max-h-96 overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/50 border-b border-zinc-800 sticky top-0">
              <tr>
                <th className="text-left px-4 py-2 text-zinc-400 font-medium">
                  Table
                </th>
                <th
                  className="text-right px-4 py-2 text-zinc-400 font-medium"
                  title="Live tuples (pg_stat_user_tables.n_live_tup)"
                >
                  Live rows
                </th>
                <th
                  className="text-right px-4 py-2 text-zinc-400 font-medium"
                  title="Dead tuples — VACUUM 대상으로 남아 있는 미회수 행 버전"
                >
                  Dead rows
                </th>
                <th
                  className="text-right px-4 py-2 text-zinc-400 font-medium"
                  title="Dead ÷ (live + dead). 30% 초과 시 bloat 심각 — VACUUM 권장"
                >
                  Dead / total
                </th>
                <th
                  className="text-right px-4 py-2 text-zinc-400 font-medium"
                  title="age(relfrozenxid) — 마지막 FREEZE 이후 트랜잭션 수. 2억 = 경고, 15억 = wraparound 위험"
                >
                  TXID age
                </th>
                <th
                  className="text-right px-4 py-2 text-zinc-400 font-medium"
                  title="마지막 autovacuum 또는 수동 VACUUM 이후 경과 시간"
                >
                  Last vacuum
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-700">
              {tables.map((t, i) => {
                const bloat = n(t.bloat_ratio);
                const bloatColor =
                  bloat > 0.3
                    ? "text-rose-400"
                    : bloat > 0.1
                      ? "text-amber-400"
                      : "text-zinc-300";
                const txidAge =
                  txidAgeByTable[`${t.schema_name}.${t.table_name}`] ?? null;
                const txidColor =
                  txidAge == null
                    ? "text-zinc-600"
                    : txidAge >= TXID_CRITICAL
                      ? "text-rose-400"
                      : txidAge >= TXID_WARN
                        ? "text-amber-400"
                        : "text-zinc-300";
                return (
                  <tr
                    key={`${t.schema_name}-${t.table_name}-${i}`}
                    className="hover:bg-zinc-900/40"
                  >
                    <td className="px-4 py-2 text-zinc-200 font-mono text-xs">
                      <span className="text-zinc-500">{t.schema_name}.</span>
                      {t.table_name}
                    </td>
                    <td
                      className="px-4 py-2 text-right text-zinc-300 font-mono text-xs tabular-nums"
                      title={fmtExact(n(t.n_live_tup))}
                    >
                      {fmtNumber(n(t.n_live_tup))}
                    </td>
                    <td
                      className="px-4 py-2 text-right text-zinc-300 font-mono text-xs tabular-nums"
                      title={fmtExact(n(t.n_dead_tup))}
                    >
                      {fmtNumber(n(t.n_dead_tup))}
                    </td>
                    <td
                      className={`px-4 py-2 text-right font-mono text-xs tabular-nums ${bloatColor}`}
                      title={`${n(t.n_dead_tup)} dead / ${
                        n(t.n_live_tup) + n(t.n_dead_tup)
                      } total`}
                    >
                      {(bloat * 100).toFixed(1)}%
                    </td>
                    <td
                      className={`px-4 py-2 text-right font-mono text-xs tabular-nums ${txidColor}`}
                      title={
                        txidAge != null
                          ? `age(relfrozenxid) = ${fmtExact(
                              txidAge,
                            )} transactions`
                          : "below warn threshold or not yet observed"
                      }
                    >
                      {txidAge != null ? fmtNumber(txidAge) : "—"}
                    </td>
                    <td className="px-4 py-2 text-right text-zinc-400 font-mono text-xs">
                      {relDays(t.last_vacuum)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
