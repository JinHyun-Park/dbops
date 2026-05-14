"use client";

import { useEffect, useState } from "react";
import { fetchIndexRecommendations } from "@/lib/api-client";
import { fmtNumber, fmtExact } from "@/lib/format";

interface Candidate {
  schema_name: string;
  table_name: string;
  seq_scan: number | string;
  idx_scan: number | string;
  seq_tup_read: number | string;
  n_live_tup: number | string;
  seq_scan_ratio: number | string;
}

function n(v: unknown) {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

export function IndexRecsPanel({ clusterId }: { clusterId: string }) {
  const [items, setItems] = useState<Candidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [minRatio, setMinRatio] = useState(0.5);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchIndexRecommendations(clusterId, minRatio)
        .then((d) => !cancelled && setItems(d.candidates || []))
        .catch(() => !cancelled && setItems([]))
        .finally(() => !cancelled && setLoading(false));
    load();
    const iv = setInterval(load, 60000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId, minRatio]);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-center justify-between">
        <div>
          <div className="text-xs text-zinc-400 uppercase tracking-wider">
            Index Recommendations
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            tables where sequential scans dominate over index scans — candidates
            for new indexes
          </div>
        </div>
        <select
          value={minRatio}
          onChange={(e) => setMinRatio(Number(e.target.value))}
          className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-xs rounded px-2 py-1"
        >
          <option value={0.3}>seq ≥ 30% of scans</option>
          <option value={0.5}>seq ≥ 50% of scans</option>
          <option value={0.8}>seq ≥ 80% of scans</option>
        </select>
      </div>
      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">Loading...</div>
      ) : items.length === 0 ? (
        <div className="p-6 text-zinc-500 text-sm">
          no index candidates — looking good!
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
                  title="Estimated live rows (pg_stat_user_tables.n_live_tup)"
                >
                  Rows
                </th>
                <th
                  className="text-right px-4 py-2 text-zinc-400 font-medium"
                  title="Number of sequential scans since stats were last reset"
                >
                  Seq scans
                </th>
                <th
                  className="text-right px-4 py-2 text-zinc-400 font-medium"
                  title="Sequential scans ÷ (sequential + index scans). High means most queries don't use indexes."
                >
                  Seq / total scans
                </th>
                <th
                  className="text-right px-4 py-2 text-zinc-400 font-medium"
                  title="Rows read by sequential scans (pg_stat_user_tables.seq_tup_read)"
                >
                  Rows scanned
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-700">
              {items.map((c, i) => {
                const ratio = n(c.seq_scan_ratio);
                const ratioColor =
                  ratio > 0.8
                    ? "text-rose-400"
                    : ratio > 0.5
                      ? "text-amber-400"
                      : "text-zinc-300";
                const rows = n(c.n_live_tup);
                const seqScan = n(c.seq_scan);
                const seqTupRead = n(c.seq_tup_read);
                return (
                  <tr
                    key={`${c.schema_name}-${c.table_name}-${i}`}
                    className="hover:bg-zinc-900/40"
                  >
                    <td className="px-4 py-2 text-zinc-200 font-mono text-xs">
                      <span className="text-zinc-500">{c.schema_name}.</span>
                      {c.table_name}
                    </td>
                    <td
                      className="px-4 py-2 text-right text-zinc-300 font-mono text-xs tabular-nums"
                      title={fmtExact(rows)}
                    >
                      {fmtNumber(rows)}
                    </td>
                    <td
                      className="px-4 py-2 text-right text-zinc-300 font-mono text-xs tabular-nums"
                      title={fmtExact(seqScan)}
                    >
                      {fmtNumber(seqScan)}
                    </td>
                    <td
                      className={`px-4 py-2 text-right font-mono text-xs tabular-nums ${ratioColor}`}
                      title={`Sequential scans are ${(ratio * 100).toFixed(
                        1,
                      )}% of all scans on this table`}
                    >
                      {(ratio * 100).toFixed(0)}%
                    </td>
                    <td
                      className="px-4 py-2 text-right text-zinc-300 font-mono text-xs tabular-nums"
                      title={`${fmtExact(
                        seqTupRead,
                      )} rows read by seq scans — high values indicate wasted IO`}
                    >
                      {fmtNumber(seqTupRead)}
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
