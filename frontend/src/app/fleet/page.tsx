"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { fetchMultiClusterOverview, fetchClusters } from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";
import { engineBadge, eolFor, EOL_STATUS_CLASSES, eolHint } from "@/lib/engine";

interface ClusterRow {
  cluster_id: string;
  engine: string;
  engine_version: string;
  status: string;
  storage_size_gb: number | string;
  cpu: number | string | null;
  aas: number | string | null;
  conn_active: number | string | null;
  conn_idle: number | string | null;
  storage_bytes: number | string | null;
  deadlocks: number | string | null;
  blocking_count: number | string | null;
}

function n(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

function severityColor(value: number, warn: number, crit: number): string {
  if (value >= crit) return "text-rose-400";
  if (value >= warn) return "text-amber-400";
  return "text-zinc-300";
}

function fmtBytes(b: number): string {
  if (b > 1e12) return `${(b / 1e12).toFixed(2)} TB`;
  if (b > 1e9) return `${(b / 1e9).toFixed(2)} GB`;
  if (b > 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  return `${b} B`;
}

export default function FleetPage() {
  const [rows, setRows] = useState<ClusterRow[]>([]);
  const [demoIds, setDemoIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<keyof ClusterRow>("cluster_id");

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      Promise.allSettled([fetchMultiClusterOverview(), fetchClusters()])
        .then(([overview, registry]) => {
          if (cancelled) return;
          if (overview.status === "fulfilled")
            setRows(overview.value.clusters || []);
          if (registry.status === "fulfilled") {
            setDemoIds(
              new Set(
                (registry.value || [])
                  .filter((c: { is_demo?: boolean }) => c.is_demo)
                  .map((c: { cluster_id: string }) => c.cluster_id),
              ),
            );
          }
          if (overview.status === "rejected")
            setErr(overview.reason?.message || String(overview.reason));
        })
        .finally(() => !cancelled && setLoading(false));
    load();
    const iv = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, []);

  const sorted = [...rows].sort((a, b) => {
    const av = a[sortKey];
    const bv = b[sortKey];
    if (typeof av === "string" && typeof bv === "string")
      return av.localeCompare(bv);
    return n(bv) - n(av);
  });

  return (
    <PageBody>
      <PageHeader
        eyebrow="monitor"
        title="Fleet overview"
        description={`${rows.length} cluster${
          rows.length === 1 ? "" : "s"
        } · auto-refresh 30s · click any row for deep dive`}
      />

      {err && (
        <div className="bg-rose-500/10 border border-rose-500/30 px-4 py-3 text-rose-300 text-sm mb-6">
          {err}
        </div>
      )}

      {loading ? (
        <div className="text-zinc-500 text-sm">loading…</div>
      ) : sorted.length === 0 ? (
        <EmptyState
          eyebrow="no clusters"
          title="No clusters registered yet"
          description="Once you register an Aurora cluster, live CPU, AAS, connection and lock metrics will stream here every 30 seconds."
          primary={{ href: "/clusters", label: "+ Register cluster" }}
        />
      ) : (
        <>
          {/* Mobile card stack — narrow screens lose the table format entirely.
              Sort controls aren't useful here (one card per row), so we just
              render the already-sorted list. */}
          <div className="md:hidden space-y-3">
            {sorted.map((c) => {
              const cpu = n(c.cpu);
              const aas = n(c.aas);
              const conn = n(c.conn_active) + n(c.conn_idle);
              const dlk = n(c.deadlocks);
              const blk = n(c.blocking_count);
              const badge = engineBadge(c.engine);
              const eol = eolFor(c.engine, c.engine_version);
              return (
                <Link
                  key={c.cluster_id}
                  href={`/dashboard?cluster=${encodeURIComponent(
                    c.cluster_id,
                  )}`}
                  className="block bg-zinc-800 border border-zinc-700 rounded-lg p-3 hover:border-amber-500/40 transition-colors"
                >
                  <div className="flex items-start justify-between gap-3 mb-2">
                    <div className="min-w-0 flex-1">
                      <div className="font-mono text-xs text-zinc-100 truncate">
                        {c.cluster_id}
                      </div>
                      <div className="flex flex-wrap items-center gap-1.5 mt-1">
                        <span
                          className={`inline-flex items-center gap-1 px-1.5 py-0.5 border text-[10px] font-mono uppercase tracking-wider ${badge.classes}`}
                        >
                          <span
                            className={`w-1 h-1 rounded-full ${badge.accent}`}
                          />
                          {badge.label}
                          <span className="text-zinc-300/80 normal-case font-normal">
                            {c.engine_version}
                          </span>
                        </span>
                        {demoIds.has(c.cluster_id) && (
                          <span className="px-1.5 py-0.5 text-[9px] font-mono uppercase bg-purple-500/15 text-purple-300 border border-purple-500/40">
                            demo
                          </span>
                        )}
                      </div>
                      {eol && (
                        <div
                          className={`text-[10px] font-mono mt-1 ${
                            EOL_STATUS_CLASSES[eol.status]
                          }`}
                        >
                          {eol.status === "expired"
                            ? `EOL · ${Math.abs(eol.days_remaining)}d past`
                            : `EOL ${eol.eol} · ${eol.days_remaining}d`}
                        </div>
                      )}
                    </div>
                    <span
                      className={`shrink-0 px-1.5 py-0.5 rounded text-[10px] ${
                        c.status === "available"
                          ? "bg-emerald-500/10 text-emerald-400"
                          : "bg-rose-500/10 text-rose-400"
                      }`}
                    >
                      {c.status || "—"}
                    </span>
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-xs font-mono tabular-nums">
                    <MobileStat
                      label="CPU"
                      value={c.cpu === null ? "—" : cpu.toFixed(1)}
                      tone={severityColor(cpu, 70, 90)}
                    />
                    <MobileStat
                      label="AAS"
                      value={c.aas === null ? "—" : aas.toFixed(2)}
                      tone={severityColor(aas, 2, 5)}
                    />
                    <MobileStat
                      label="Conn"
                      value={conn ? String(conn) : "—"}
                    />
                    <MobileStat
                      label="Storage"
                      value={
                        c.storage_bytes ? fmtBytes(n(c.storage_bytes)) : "—"
                      }
                    />
                    <MobileStat
                      label="Deadlocks"
                      value={dlk ? String(dlk) : "—"}
                      tone={dlk ? "text-rose-400" : "text-zinc-500"}
                    />
                    <MobileStat
                      label="Blocks"
                      value={blk ? String(blk) : "—"}
                      tone={blk ? "text-rose-400" : "text-zinc-500"}
                    />
                  </div>
                </Link>
              );
            })}
          </div>

          {/* Desktop table — unchanged from the original 9-column layout. */}
          <div className="hidden md:block bg-zinc-800 border border-zinc-700 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-900/50 border-b border-zinc-700">
                <tr>
                  <ThSort
                    label="Cluster"
                    onClick={() => setSortKey("cluster_id")}
                  />
                  <ThSort label="Engine" onClick={() => setSortKey("engine")} />
                  <ThSort label="Status" onClick={() => setSortKey("status")} />
                  <ThSort
                    label="CPU %"
                    align="right"
                    onClick={() => setSortKey("cpu")}
                  />
                  <ThSort
                    label="AAS"
                    align="right"
                    onClick={() => setSortKey("aas")}
                  />
                  <ThSort
                    label="Conn"
                    align="right"
                    onClick={() => setSortKey("conn_active")}
                  />
                  <ThSort
                    label="Storage"
                    align="right"
                    onClick={() => setSortKey("storage_bytes")}
                  />
                  <ThSort
                    label="Deadlocks"
                    align="right"
                    onClick={() => setSortKey("deadlocks")}
                  />
                  <ThSort
                    label="Blocks"
                    align="right"
                    onClick={() => setSortKey("blocking_count")}
                  />
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-700">
                {sorted.map((c) => {
                  const cpu = n(c.cpu);
                  const aas = n(c.aas);
                  const conn = n(c.conn_active) + n(c.conn_idle);
                  const dlk = n(c.deadlocks);
                  const blk = n(c.blocking_count);
                  return (
                    <tr key={c.cluster_id} className="hover:bg-zinc-900/40">
                      <td className="px-3 py-2 text-zinc-200 font-mono text-xs">
                        <div className="flex items-center gap-2">
                          <Link
                            href={`/dashboard?cluster=${encodeURIComponent(
                              c.cluster_id,
                            )}`}
                            className="hover:text-sky-400 underline-offset-2 hover:underline"
                          >
                            {c.cluster_id}
                          </Link>
                          {demoIds.has(c.cluster_id) && (
                            <span className="px-1.5 py-0.5 text-[9px] font-mono tracking-wider uppercase bg-purple-500/15 text-purple-300 border border-purple-500/40">
                              demo
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-3 py-2">
                        {(() => {
                          const badge = engineBadge(c.engine);
                          const eol = eolFor(c.engine, c.engine_version);
                          return (
                            <div className="flex flex-col items-start gap-1">
                              <span
                                className={`inline-flex items-center gap-1.5 px-1.5 py-0.5 border text-[10px] font-mono uppercase tracking-wider ${badge.classes}`}
                                title={`${c.engine} ${c.engine_version}`}
                              >
                                <span
                                  className={`w-1 h-1 rounded-full ${badge.accent}`}
                                />
                                {badge.label}
                                <span className="text-zinc-300/80 normal-case font-normal">
                                  {c.engine_version}
                                </span>
                              </span>
                              {eol && (
                                <span
                                  className={`text-[10px] font-mono ${
                                    EOL_STATUS_CLASSES[eol.status]
                                  }`}
                                  title={eolHint(eol)}
                                >
                                  {eol.status === "expired"
                                    ? `EOL · ${Math.abs(
                                        eol.days_remaining,
                                      )}d past`
                                    : `EOL ${eol.eol} · ${eol.days_remaining}d`}
                                </span>
                              )}
                            </div>
                          );
                        })()}
                      </td>
                      <td className="px-3 py-2">
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] ${
                            c.status === "available"
                              ? "bg-emerald-500/10 text-emerald-400"
                              : "bg-rose-500/10 text-rose-400"
                          }`}
                        >
                          {c.status}
                        </span>
                      </td>
                      <td
                        className={`px-3 py-2 text-right font-mono text-xs ${severityColor(
                          cpu,
                          70,
                          90,
                        )}`}
                      >
                        {c.cpu === null ? "-" : cpu.toFixed(1)}
                      </td>
                      <td
                        className={`px-3 py-2 text-right font-mono text-xs ${severityColor(
                          aas,
                          2,
                          5,
                        )}`}
                      >
                        {c.aas === null ? "-" : aas.toFixed(2)}
                      </td>
                      <td className="px-3 py-2 text-right text-zinc-300 font-mono text-xs">
                        {conn || "-"}
                      </td>
                      <td className="px-3 py-2 text-right text-zinc-300 font-mono text-xs">
                        {c.storage_bytes ? fmtBytes(n(c.storage_bytes)) : "-"}
                      </td>
                      <td
                        className={`px-3 py-2 text-right font-mono text-xs ${severityColor(
                          dlk,
                          1,
                          5,
                        )}`}
                      >
                        {dlk || "-"}
                      </td>
                      <td
                        className={`px-3 py-2 text-right font-mono text-xs ${severityColor(
                          blk,
                          1,
                          3,
                        )}`}
                      >
                        {blk || "-"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </PageBody>
  );
}

function ThSort({
  label,
  align = "left",
  onClick,
}: {
  label: string;
  align?: "left" | "right";
  onClick: () => void;
}) {
  return (
    <th
      onClick={onClick}
      className={`px-3 py-2 text-zinc-400 font-medium cursor-pointer hover:text-zinc-200 text-${align}`}
    >
      {label}
    </th>
  );
}

function MobileStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: string;
}) {
  return (
    <div className="flex flex-col">
      <span className="text-[9px] uppercase tracking-wider text-zinc-500">
        {label}
      </span>
      <span className={tone || "text-zinc-200"}>{value}</span>
    </div>
  );
}
