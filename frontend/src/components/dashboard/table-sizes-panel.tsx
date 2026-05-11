"use client";

import { useEffect, useState } from "react";
import { fetchTableSizes } from "@/lib/api-client";

interface Table {
  schema_name: string;
  table_name: string;
  n_live_tup: number | string;
  total_bytes: number | string;
  table_bytes: number | string;
  index_bytes: number | string;
  index_ratio: number | string;
}

function n(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

function fmtBytes(b: number): string {
  if (b > 1e12) return `${(b / 1e12).toFixed(2)} TB`;
  if (b > 1e9) return `${(b / 1e9).toFixed(2)} GB`;
  if (b > 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  if (b > 1e3) return `${(b / 1e3).toFixed(1)} KB`;
  return `${b} B`;
}

export function TableSizesPanel({ clusterId }: { clusterId: string }) {
  const [tables, setTables] = useState<Table[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchTableSizes(clusterId)
        .then((d) => !cancelled && setTables(d.tables || []))
        .catch(() => !cancelled && setTables([]))
        .finally(() => !cancelled && setLoading(false));
    load();
    const iv = setInterval(load, 60000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId]);

  const totalBytes = tables.reduce((s, t) => s + n(t.total_bytes), 0);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-center justify-between">
        <div>
          <div className="text-xs text-zinc-400 uppercase tracking-wider">Table Sizes</div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            total {fmtBytes(totalBytes)} across {tables.length} tables (top 30)
          </div>
        </div>
      </div>
      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">Loading...</div>
      ) : tables.length === 0 ? (
        <div className="p-6 text-zinc-500 text-sm">
          no table size data yet (PG only, next ETL run will include sizes)
        </div>
      ) : (
        <div className="max-h-96 overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/50 border-b border-zinc-800 sticky top-0">
              <tr>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium">Table</th>
                <th className="text-right px-3 py-2 text-zinc-400 font-medium">Rows</th>
                <th className="text-right px-3 py-2 text-zinc-400 font-medium">Heap</th>
                <th className="text-right px-3 py-2 text-zinc-400 font-medium">Indexes</th>
                <th className="text-right px-3 py-2 text-zinc-400 font-medium">Total</th>
                <th className="text-right px-3 py-2 text-zinc-400 font-medium">Index %</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-700">
              {tables.map((t, i) => {
                const total = n(t.total_bytes);
                const pct = totalBytes > 0 ? (total / totalBytes) * 100 : 0;
                const idxRatio = n(t.index_ratio) * 100;
                return (
                  <tr key={`${t.schema_name}-${t.table_name}-${i}`} className="hover:bg-zinc-900/40 relative">
                    <td className="px-3 py-2 text-zinc-200 font-mono text-xs">
                      <span className="text-zinc-500">{t.schema_name}.</span>
                      {t.table_name}
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-300 font-mono text-xs">
                      {n(t.n_live_tup).toLocaleString()}
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-300 font-mono text-xs">
                      {fmtBytes(n(t.table_bytes))}
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-300 font-mono text-xs">
                      {fmtBytes(n(t.index_bytes))}
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-100 font-mono text-xs relative">
                      <div className="relative z-10">{fmtBytes(total)}</div>
                      <div
                        className="absolute inset-y-0 right-0 bg-sky-500/10"
                        style={{ width: `${pct}%` }}
                      />
                    </td>
                    <td
                      className={`px-3 py-2 text-right font-mono text-xs ${
                        idxRatio > 80 ? "text-rose-400" : idxRatio > 50 ? "text-amber-400" : "text-zinc-300"
                      }`}
                    >
                      {idxRatio.toFixed(0)}%
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
