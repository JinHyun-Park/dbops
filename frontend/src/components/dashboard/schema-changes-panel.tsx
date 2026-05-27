"use client";

import { useEffect, useState } from "react";
import { fetchSchemaChanges } from "@/lib/api-client";
import { fmtExact, fmtNumber } from "@/lib/format";

interface Change {
  schema_name: string;
  table_name: string;
  baseline_rows: number | string | null;
  current_rows: number | string | null;
  change_type: "created" | "dropped" | "changed";
  baseline_time: string | null;
  current_time: string | null;
}

function n(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

const TYPE_STYLES: Record<string, { color: string; bg: string; icon: string }> =
  {
    created: { color: "text-emerald-400", bg: "bg-emerald-500/10", icon: "＋" },
    dropped: { color: "text-rose-400", bg: "bg-rose-500/10", icon: "－" },
    changed: { color: "text-amber-400", bg: "bg-amber-500/10", icon: "Δ" },
  };

export function SchemaChangesPanel({ clusterId }: { clusterId: string }) {
  const [changes, setChanges] = useState<Change[]>([]);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchSchemaChanges(clusterId, days)
      .then((d) => !cancelled && setChanges(d.changes || []))
      .catch(() => !cancelled && setChanges([]))
      .finally(() => !cancelled && setLoading(false));
  }, [clusterId, days]);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-center justify-between">
        <div>
          <div className="text-xs text-zinc-400 uppercase tracking-wider">
            Schema Changes
            {changes.length > 0 && (
              <span className="ml-2 px-1.5 py-0.5 bg-amber-500/20 text-amber-300 rounded text-[10px]">
                {changes.length}
              </span>
            )}
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            테이블 생성·삭제 또는 행 수가 크게 변한 항목
          </div>
        </div>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-xs rounded px-2 py-1"
        >
          <option value={1}>최근 1일</option>
          <option value={7}>최근 7일</option>
          <option value={30}>최근 30일</option>
        </select>
      </div>
      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">불러오는 중…</div>
      ) : changes.length === 0 ? (
        <div className="p-6 text-zinc-500 text-sm">감지된 스키마 변경 없음</div>
      ) : (
        <div className="divide-y divide-zinc-700">
          {changes.map((c, i) => {
            const style = TYPE_STYLES[c.change_type] || TYPE_STYLES.changed;
            const baseline = n(c.baseline_rows);
            const current = n(c.current_rows);
            const delta = current - baseline;
            const deltaPct =
              baseline > 0 ? ((delta / baseline) * 100).toFixed(0) : "∞";
            return (
              <div
                key={`${c.schema_name}-${c.table_name}-${i}`}
                className={`px-4 py-2.5 ${style.bg}`}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span className={`text-lg font-mono ${style.color}`}>
                      {style.icon}
                    </span>
                    <span
                      className={`text-[10px] uppercase ${style.color} font-medium`}
                    >
                      {c.change_type}
                    </span>
                    <span className="text-sm font-mono text-zinc-200">
                      <span className="text-zinc-500">{c.schema_name}.</span>
                      {c.table_name}
                    </span>
                  </div>
                  <div className="text-xs font-mono text-zinc-400 tabular-nums">
                    {c.change_type === "created" && (
                      <span
                        className="text-emerald-400"
                        title={`${fmtExact(current)} 행`}
                      >
                        {fmtNumber(current)} 행
                      </span>
                    )}
                    {c.change_type === "dropped" && (
                      <span
                        className="text-rose-400"
                        title={`${fmtExact(baseline)} 행 손실`}
                      >
                        {fmtNumber(baseline)} 행 손실
                      </span>
                    )}
                    {c.change_type === "changed" && (
                      <>
                        <span title={fmtExact(baseline)}>
                          {fmtNumber(baseline)}
                        </span>
                        <span className="text-zinc-600 mx-1.5">→</span>
                        <span title={fmtExact(current)}>
                          {fmtNumber(current)}
                        </span>
                        <span
                          className={`ml-2 ${
                            delta > 0 ? "text-emerald-400" : "text-rose-400"
                          }`}
                          title={`증감 ${delta > 0 ? "+" : ""}${fmtExact(
                            delta,
                          )}`}
                        >
                          ({delta > 0 ? "+" : ""}
                          {deltaPct}%)
                        </span>
                      </>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
