"use client";

import { Fragment, useEffect, useState } from "react";
import { fetchTableIndexes, fetchTableSizes, type TableIndex } from "@/lib/api-client";
import { fmtBytes, fmtExact, fmtNumber } from "@/lib/format";

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

export function TableSizesPanel({ clusterId }: { clusterId: string }) {
  const [tables, setTables] = useState<Table[]>([]);
  const [loading, setLoading] = useState(true);
  // Expansion state per table — keyed by `schema.table`. value is the loaded
  // indexes list (or {loading:true} / {error:string}). Indexes are queried
  // lazily against the live cluster only when a row is expanded.
  const [expanded, setExpanded] = useState<
    Record<string, { loading?: boolean; indexes?: TableIndex[]; error?: string }>
  >({});

  const toggleExpand = (schema: string, table: string) => {
    const key = `${schema}.${table}`;
    const isExpanding = !expanded[key];
    setExpanded((prev) => {
      if (prev[key]) {
        const next = { ...prev };
        delete next[key];
        return next;
      }
      return { ...prev, [key]: { loading: true } };
    });
    // Side-effect fetch — separated from setState so React batching doesn't
    // skip it. We compute isExpanding from the current closure (pre-toggle).
    if (isExpanding) {
      fetchTableIndexes(clusterId, schema, table)
        .then((r) =>
          setExpanded((p) => ({ ...p, [key]: { indexes: r.indexes } })),
        )
        .catch((e) =>
          setExpanded((p) => ({
            ...p,
            [key]: { error: e instanceof Error ? e.message : "fetch failed" },
          })),
        );
    }
  };

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
                <th
                  className="text-right px-3 py-2 text-zinc-400 font-medium"
                  title="Estimated live row count (pg_stat_user_tables.n_live_tup)"
                >
                  Rows
                </th>
                <th
                  className="text-right px-3 py-2 text-zinc-400 font-medium"
                  title="On-disk heap (table data without indexes/TOAST)"
                >
                  Heap
                </th>
                <th
                  className="text-right px-3 py-2 text-zinc-400 font-medium"
                  title="Combined size of all indexes on this table"
                >
                  Indexes
                </th>
                <th
                  className="text-right px-3 py-2 text-zinc-400 font-medium"
                  title="Heap + indexes + TOAST"
                >
                  Total
                </th>
                <th
                  className="text-right px-3 py-2 text-zinc-400 font-medium"
                  title="Index bytes ÷ total bytes for this row. >50% means indexes are bigger than the heap — review for redundant indexes."
                >
                  Indexes / total
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-700">
              {tables.map((t, i) => {
                const total = n(t.total_bytes);
                const pct = totalBytes > 0 ? (total / totalBytes) * 100 : 0;
                const idxRatio = n(t.index_ratio) * 100;
                const rowCount = n(t.n_live_tup);
                const expandKey = `${t.schema_name}.${t.table_name}`;
                const expand = expanded[expandKey];
                const isOpen = !!expand;
                return (
                  <Fragment key={`${t.schema_name}-${t.table_name}-${i}`}>
                  <tr
                    className="hover:bg-zinc-900/40 relative cursor-pointer"
                    onClick={() => toggleExpand(t.schema_name, t.table_name)}
                    title="Click to view indexes on this table"
                  >
                    <td className="px-3 py-2 text-zinc-200 font-mono text-xs">
                      <span className="text-zinc-500 mr-1.5 inline-block w-3">{isOpen ? "▾" : "▸"}</span>
                      <span className="text-zinc-500">{t.schema_name}.</span>
                      {t.table_name}
                    </td>
                    <td
                      className="px-3 py-2 text-right text-zinc-300 font-mono text-xs tabular-nums"
                      title={`${fmtExact(rowCount)} rows`}
                    >
                      {fmtNumber(rowCount)}
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-300 font-mono text-xs">
                      {fmtBytes(n(t.table_bytes))}
                    </td>
                    <td className="px-3 py-2 text-right text-zinc-300 font-mono text-xs">
                      {fmtBytes(n(t.index_bytes))}
                    </td>
                    <td
                      className="px-3 py-2 text-right text-zinc-100 font-mono text-xs relative"
                      title={`${pct.toFixed(1)}% of total ${fmtBytes(totalBytes)} across all tables`}
                    >
                      <div className="relative z-10">{fmtBytes(total)}</div>
                      <div
                        className="absolute inset-y-0 right-0 bg-sky-500/10"
                        style={{ width: `${pct}%` }}
                        aria-hidden="true"
                      />
                    </td>
                    <td
                      className={`px-3 py-2 text-right font-mono text-xs ${
                        idxRatio > 70 ? "text-rose-400" : idxRatio > 50 ? "text-amber-400" : "text-zinc-300"
                      }`}
                      title={`Indexes ${fmtBytes(n(t.index_bytes))} of total ${fmtBytes(total)}`}
                    >
                      {idxRatio.toFixed(0)}%
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="bg-zinc-950/40">
                      <td colSpan={6} className="px-6 py-3">
                        <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 mb-2">
                          indexes on {t.schema_name}.{t.table_name}
                        </div>
                        {expand?.loading && (
                          <div className="text-xs text-zinc-500">Loading…</div>
                        )}
                        {expand?.error && (
                          <div className="text-xs text-rose-400 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
                            {expand.error}
                          </div>
                        )}
                        {expand?.indexes && expand.indexes.length === 0 && (
                          <div className="text-xs text-zinc-500">no indexes (table is heap-only)</div>
                        )}
                        {expand?.indexes && expand.indexes.length > 0 && (
                          <table className="w-full text-xs">
                            <thead className="text-[10px] uppercase tracking-wider text-zinc-500">
                              <tr>
                                <th className="text-left py-1 pr-3 font-medium">Index</th>
                                <th className="text-left py-1 pr-3 font-medium">Definition</th>
                                <th className="text-right py-1 px-3 font-medium" title="Times this index was used to satisfy a query (pg_stat_user_indexes.idx_scan)">Scans</th>
                                <th className="text-right py-1 pl-3 font-medium" title="Disk size of the index">Size</th>
                              </tr>
                            </thead>
                            <tbody>
                              {expand.indexes.map((idx) => (
                                <tr key={idx.index_name} className="border-t border-zinc-800/60">
                                  <td className="py-1 pr-3 font-mono text-zinc-200 align-top">
                                    <div className="flex items-center gap-1.5">
                                      <span>{idx.index_name}</span>
                                      {idx.is_primary && (
                                        <span className="text-[9px] px-1 py-0.5 border border-amber-500/40 text-amber-300 rounded-sm" title="primary key">PK</span>
                                      )}
                                      {!idx.is_primary && idx.is_unique && (
                                        <span className="text-[9px] px-1 py-0.5 border border-sky-500/40 text-sky-300 rounded-sm" title="unique index">UQ</span>
                                      )}
                                      {!idx.is_valid && (
                                        <span className="text-[9px] px-1 py-0.5 border border-rose-500/40 text-rose-300 rounded-sm" title="index is INVALID (concurrent build failed?)">!</span>
                                      )}
                                      {idx.idx_scan === 0 && (
                                        <span className="text-[9px] px-1 py-0.5 border border-zinc-700 text-zinc-500 rounded-sm" title="never used since stats reset — candidate for DROP">unused</span>
                                      )}
                                    </div>
                                  </td>
                                  <td className="py-1 pr-3 font-mono text-zinc-400 align-top break-all">
                                    {idx.definition.replace(/^CREATE (UNIQUE )?INDEX \S+ /, "")}
                                  </td>
                                  <td className="py-1 px-3 text-right font-mono text-zinc-300 tabular-nums align-top" title={fmtExact(idx.idx_scan)}>
                                    {fmtNumber(idx.idx_scan)}
                                  </td>
                                  <td className="py-1 pl-3 text-right font-mono text-zinc-300 tabular-nums align-top">
                                    {fmtBytes(idx.bytes)}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        )}
                      </td>
                    </tr>
                  )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
