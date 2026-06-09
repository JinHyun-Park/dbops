"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { fetchMultiClusterOverview, fetchClusters } from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";
import {
  engineBadge,
  eolFor,
  EOL_STATUS_CLASSES,
  eolHint,
  type EolInfo,
} from "@/lib/engine";

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

type Level = "critical" | "warning" | "ok";

// A row decorated with the derived triage signals so we sort/filter/group on
// computed severity, not raw alphabetical cluster_id.
interface Decorated {
  row: ClusterRow;
  level: Level;
  heat: number; // tiebreak within a level: higher = worse
  reasons: string[]; // why it's at this level (tooltip)
  eol: EolInfo | null;
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

// Per-cluster triage level from the SAME thresholds the table cells use, so the
// summary band, the row dot, and the cell colors all agree. A missing metric
// never counts against a cluster (null != a problem).
function triage(c: ClusterRow, eol: EolInfo | null): Decorated {
  const cpu = c.cpu === null ? null : n(c.cpu);
  const aas = c.aas === null ? null : n(c.aas);
  const dlk = n(c.deadlocks);
  const blk = n(c.blocking_count);
  const reasons: string[] = [];
  let level: Level = "ok";

  const crit: string[] = [];
  if (c.status && c.status !== "available") crit.push(`status=${c.status}`);
  if (cpu !== null && cpu >= 90) crit.push(`CPU ${cpu.toFixed(0)}%`);
  if (aas !== null && aas >= 5) crit.push(`AAS ${aas.toFixed(1)}`);
  if (dlk >= 5) crit.push(`${dlk} deadlocks`);
  if (blk >= 3) crit.push(`${blk} blocking`);
  if (eol && (eol.status === "expired" || eol.status === "imminent"))
    crit.push(eol.status === "expired" ? "EOL passed" : "EOL imminent");

  const warn: string[] = [];
  if (cpu !== null && cpu >= 70) warn.push(`CPU ${cpu.toFixed(0)}%`);
  if (aas !== null && aas >= 2) warn.push(`AAS ${aas.toFixed(1)}`);
  if (dlk >= 1) warn.push(`${dlk} deadlocks`);
  if (blk >= 1) warn.push(`${blk} blocking`);
  if (eol && eol.status === "soon") warn.push("EOL < 1y");

  if (crit.length) {
    level = "critical";
    reasons.push(...crit);
  } else if (warn.length) {
    level = "warning";
    reasons.push(...warn);
  }

  // Heat orders rows within a level: weight the scarier signals higher.
  const heat =
    (cpu ?? 0) +
    (aas ?? 0) * 15 +
    dlk * 8 +
    blk * 12 +
    (c.status && c.status !== "available" ? 100 : 0) +
    (eol?.status === "expired" ? 60 : eol?.status === "imminent" ? 30 : 0);

  return { row: c, level, heat, reasons, eol };
}

const LEVEL_RANK: Record<Level, number> = { critical: 2, warning: 1, ok: 0 };

export default function FleetPage() {
  const [rows, setRows] = useState<ClusterRow[]>([]);
  const [demoIds, setDemoIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  // sortKey "severity" (default) = triage-first; any column key overrides it.
  const [sortKey, setSortKey] = useState<"severity" | keyof ClusterRow>(
    "severity",
  );

  // Filters (persisted to the URL so a view is shareable/bookmarkable).
  const [q, setQ] = useState("");
  const [engine, setEngine] = useState("");
  const [status, setStatus] = useState("");
  const [level, setLevel] = useState<"" | Level>("");
  const [eolOnly, setEolOnly] = useState(false);

  // Hydrate filters from the URL once on mount (client-only — static export).
  useEffect(() => {
    const sp = new URLSearchParams(window.location.search);
    setQ(sp.get("q") || "");
    setEngine(sp.get("engine") || "");
    setStatus(sp.get("status") || "");
    setLevel((sp.get("level") as Level) || "");
    setEolOnly(sp.get("eol") === "1");
  }, []);

  // Reflect filters back into the URL (replace, not push — no history spam).
  useEffect(() => {
    const sp = new URLSearchParams();
    if (q) sp.set("q", q);
    if (engine) sp.set("engine", engine);
    if (status) sp.set("status", status);
    if (level) sp.set("level", level);
    if (eolOnly) sp.set("eol", "1");
    const qs = sp.toString();
    window.history.replaceState(
      null,
      "",
      qs ? `?${qs}` : window.location.pathname,
    );
  }, [q, engine, status, level, eolOnly]);

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

  const decorated = useMemo(
    () => rows.map((c) => triage(c, eolFor(c.engine, c.engine_version))),
    [rows],
  );

  // Fleet-wide rollup for the summary band (computed over ALL rows, not the
  // filtered view, so the totals stay stable as you filter).
  const counts = useMemo(() => {
    let critical = 0,
      warning = 0,
      ok = 0,
      eolAttn = 0;
    for (const d of decorated) {
      if (d.level === "critical") critical++;
      else if (d.level === "warning") warning++;
      else ok++;
      if (d.eol && d.eol.status !== "safe") eolAttn++;
    }
    return { total: decorated.length, critical, warning, ok, eolAttn };
  }, [decorated]);

  const engines = useMemo(
    () => Array.from(new Set(rows.map((r) => r.engine).filter(Boolean))).sort(),
    [rows],
  );

  const view = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const filtered = decorated.filter((d) => {
      const c = d.row;
      if (ql && !c.cluster_id.toLowerCase().includes(ql)) return false;
      if (engine && c.engine !== engine) return false;
      if (status === "available" && c.status !== "available") return false;
      if (status === "other" && c.status === "available") return false;
      if (level && d.level !== level) return false;
      if (eolOnly && !(d.eol && d.eol.status !== "safe")) return false;
      return true;
    });
    filtered.sort((a, b) => {
      if (sortKey === "severity") {
        if (LEVEL_RANK[a.level] !== LEVEL_RANK[b.level])
          return LEVEL_RANK[b.level] - LEVEL_RANK[a.level];
        return b.heat - a.heat;
      }
      const av = a.row[sortKey];
      const bv = b.row[sortKey];
      if (typeof av === "string" && typeof bv === "string")
        return av.localeCompare(bv);
      return n(bv) - n(av);
    });
    return filtered;
  }, [decorated, q, engine, status, level, eolOnly, sortKey]);

  const filtersActive = !!q || !!engine || !!status || !!level || eolOnly;
  const clearAll = () => {
    setQ("");
    setEngine("");
    setStatus("");
    setLevel("");
    setEolOnly(false);
  };

  return (
    <PageBody>
      <PageHeader
        eyebrow="모니터"
        title="Fleet 개요"
        description={`총 ${counts.total}개 클러스터 · 위험도 순 정렬 · 30초마다 자동 새로고침`}
      />

      {err && (
        <div className="bg-rose-500/10 border border-rose-500/30 px-4 py-3 text-rose-300 text-sm mb-6">
          {err}
        </div>
      )}

      {/* Triage summary band — clickable to filter the table to that bucket. */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2 mb-4">
        <SummaryChip
          label="전체"
          value={counts.total}
          tone="zinc"
          active={!level && !eolOnly}
          onClick={() => {
            setLevel("");
            setEolOnly(false);
          }}
        />
        <SummaryChip
          label="Critical"
          value={counts.critical}
          tone="rose"
          active={level === "critical"}
          onClick={() => setLevel(level === "critical" ? "" : "critical")}
        />
        <SummaryChip
          label="Warning"
          value={counts.warning}
          tone="amber"
          active={level === "warning"}
          onClick={() => setLevel(level === "warning" ? "" : "warning")}
        />
        <SummaryChip
          label="정상"
          value={counts.ok}
          tone="emerald"
          active={level === "ok"}
          onClick={() => setLevel(level === "ok" ? "" : "ok")}
        />
        <SummaryChip
          label="EOL 주의"
          value={counts.eolAttn}
          tone={counts.eolAttn ? "amber" : "zinc"}
          active={eolOnly}
          onClick={() => setEolOnly((v) => !v)}
        />
      </div>

      {/* Controls: search + facets + active-filter clear. */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="클러스터 검색…"
          className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2.5 py-1.5 font-mono w-56 focus:outline-none focus:ring-1 focus:ring-amber-500"
        />
        <FacetSelect value={engine} onChange={setEngine} label="Engine">
          <option value="">모든 엔진</option>
          {engines.map((e) => (
            <option key={e} value={e}>
              {e.replace("aurora-", "")}
            </option>
          ))}
        </FacetSelect>
        <FacetSelect value={status} onChange={setStatus} label="Status">
          <option value="">모든 상태</option>
          <option value="available">available</option>
          <option value="other">available 아님</option>
        </FacetSelect>
        {filtersActive && (
          <button
            onClick={clearAll}
            className="text-[11px] text-zinc-400 hover:text-zinc-200 underline underline-offset-2"
          >
            필터 초기화
          </button>
        )}
        <span className="text-[11px] text-zinc-500 font-mono ml-auto">
          {view.length} / {counts.total} 표시
        </span>
      </div>

      {loading ? (
        <div className="text-zinc-500 text-sm">불러오는 중…</div>
      ) : counts.total === 0 ? (
        <EmptyState
          eyebrow="클러스터 없음"
          title="아직 등록된 클러스터가 없습니다"
          description="Aurora 클러스터를 등록하면 CPU, AAS, connection, lock 등 메트릭이 30초 주기로 이 페이지에 스트리밍됩니다."
          primary={{ href: "/clusters", label: "+ 클러스터 등록" }}
        />
      ) : view.length === 0 ? (
        <div className="bg-zinc-800/40 border border-zinc-700 rounded-lg px-4 py-8 text-center text-zinc-500 text-sm">
          필터에 맞는 클러스터가 없습니다.{" "}
          <button
            onClick={clearAll}
            className="text-amber-400 hover:text-amber-300 underline underline-offset-2"
          >
            초기화
          </button>
        </div>
      ) : (
        <>
          {/* Mobile card stack — severity-sorted, with a left accent bar. */}
          <div className="md:hidden space-y-3">
            {view.map((d) => {
              const c = d.row;
              const cpu = n(c.cpu);
              const aas = n(c.aas);
              const conn = n(c.conn_active) + n(c.conn_idle);
              const dlk = n(c.deadlocks);
              const blk = n(c.blocking_count);
              const badge = engineBadge(c.engine);
              return (
                <Link
                  key={c.cluster_id}
                  href={`/dashboard?cluster=${encodeURIComponent(
                    c.cluster_id,
                  )}`}
                  className={`block bg-zinc-800 border border-zinc-700 border-l-2 ${
                    LEVEL_ACCENT[d.level]
                  } rounded-lg p-3 hover:border-amber-500/40 transition-colors`}
                >
                  <div className="flex items-start justify-between gap-3 mb-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <SeverityDot level={d.level} reasons={d.reasons} />
                        <span className="font-mono text-xs text-zinc-100 truncate">
                          {c.cluster_id}
                        </span>
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
                      {d.eol && (
                        <div
                          className={`text-[10px] font-mono mt-1 ${
                            EOL_STATUS_CLASSES[d.eol.status]
                          }`}
                        >
                          {d.eol.status === "expired"
                            ? `EOL · ${Math.abs(d.eol.days_remaining)}d past`
                            : `EOL ${d.eol.eol} · ${d.eol.days_remaining}d`}
                        </div>
                      )}
                    </div>
                    <span
                      className={`shrink-0 px-1.5 py-0.5 rounded text-[10px] ${statusBadge(
                        c.status,
                      )}`}
                    >
                      {c.status || "unknown"}
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

          {/* Desktop table — severity column leads; default sort is severity. */}
          <div className="hidden md:block bg-zinc-800 border border-zinc-700 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-900/50 border-b border-zinc-700">
                <tr>
                  <ThSort
                    label="!"
                    sortKey="severity"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                  <ThSort
                    label="Cluster"
                    sortKey="cluster_id"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                  <ThSort
                    label="Engine"
                    sortKey="engine"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                  <ThSort
                    label="Status"
                    sortKey="status"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                  <ThSort
                    label="CPU %"
                    align="right"
                    sortKey="cpu"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                  <ThSort
                    label="AAS"
                    align="right"
                    sortKey="aas"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                  <ThSort
                    label="Conn"
                    align="right"
                    sortKey="conn_active"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                  <ThSort
                    label="Storage"
                    align="right"
                    sortKey="storage_bytes"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                  <ThSort
                    label="Deadlocks"
                    align="right"
                    sortKey="deadlocks"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                  <ThSort
                    label="Blocks"
                    align="right"
                    sortKey="blocking_count"
                    active={sortKey}
                    onClick={setSortKey}
                  />
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-700">
                {view.map((d) => {
                  const c = d.row;
                  const cpu = n(c.cpu);
                  const aas = n(c.aas);
                  const conn = n(c.conn_active) + n(c.conn_idle);
                  const dlk = n(c.deadlocks);
                  const blk = n(c.blocking_count);
                  return (
                    <tr key={c.cluster_id} className="hover:bg-zinc-900/40">
                      <td className="px-2 py-2">
                        <SeverityDot level={d.level} reasons={d.reasons} />
                      </td>
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
                              {d.eol && (
                                <span
                                  className={`text-[10px] font-mono ${
                                    EOL_STATUS_CLASSES[d.eol.status]
                                  }`}
                                  title={eolHint(d.eol)}
                                >
                                  {d.eol.status === "expired"
                                    ? `EOL · ${Math.abs(
                                        d.eol.days_remaining,
                                      )}d past`
                                    : `EOL ${d.eol.eol} · ${d.eol.days_remaining}d`}
                                </span>
                              )}
                            </div>
                          );
                        })()}
                      </td>
                      <td className="px-3 py-2">
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] ${statusBadge(
                            c.status,
                          )}`}
                        >
                          {c.status || "unknown"}
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

// available → green, a present non-available status → red, but a MISSING status
// (not collected yet) is neutral, not an outage-red false alarm.
function statusBadge(status: string): string {
  if (status === "available") return "bg-emerald-500/10 text-emerald-400";
  if (status) return "bg-rose-500/10 text-rose-400";
  return "bg-zinc-700/40 text-zinc-400";
}

const LEVEL_ACCENT: Record<Level, string> = {
  critical: "border-l-rose-500",
  warning: "border-l-amber-500",
  ok: "border-l-zinc-700",
};

const SUMMARY_TONES: Record<string, { box: string; value: string }> = {
  zinc: { box: "border-zinc-700 bg-zinc-900/60", value: "text-zinc-200" },
  rose: { box: "border-rose-500/40 bg-rose-500/10", value: "text-rose-300" },
  amber: {
    box: "border-amber-500/40 bg-amber-500/10",
    value: "text-amber-300",
  },
  emerald: {
    box: "border-emerald-500/40 bg-emerald-500/10",
    value: "text-emerald-300",
  },
};

function SummaryChip({
  label,
  value,
  tone,
  active,
  onClick,
}: {
  label: string;
  value: number;
  tone: keyof typeof SUMMARY_TONES | string;
  active: boolean;
  onClick: () => void;
}) {
  const t = SUMMARY_TONES[tone] || SUMMARY_TONES.zinc;
  return (
    <button
      onClick={onClick}
      className={`border px-3 py-2 text-left transition-colors ${t.box} ${
        active ? "ring-1 ring-amber-500/70" : "hover:border-zinc-500"
      }`}
    >
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className={`text-xl font-mono tabular-nums ${t.value}`}>{value}</div>
    </button>
  );
}

const DOT_TONE: Record<Level, string> = {
  critical: "bg-rose-500",
  warning: "bg-amber-500",
  ok: "bg-emerald-500/50",
};

function SeverityDot({ level, reasons }: { level: Level; reasons: string[] }) {
  const title =
    level === "ok" ? "정상" : `${level.toUpperCase()} — ${reasons.join(", ")}`;
  return (
    <span
      title={title}
      className={`inline-block w-2 h-2 rounded-full ${DOT_TONE[level]}`}
    />
  );
}

function FacetSelect({
  value,
  onChange,
  label,
  children,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-amber-500"
    >
      {children}
    </select>
  );
}

function ThSort({
  label,
  align = "left",
  sortKey,
  active,
  onClick,
}: {
  label: string;
  align?: "left" | "right";
  sortKey: "severity" | keyof ClusterRow;
  active: "severity" | keyof ClusterRow;
  onClick: (k: "severity" | keyof ClusterRow) => void;
}) {
  const isActive = active === sortKey;
  return (
    <th
      onClick={() => onClick(sortKey)}
      className={`px-3 py-2 font-medium cursor-pointer text-${align} ${
        isActive ? "text-amber-300" : "text-zinc-400 hover:text-zinc-200"
      }`}
    >
      {label}
      {isActive && <span className="ml-1 text-[9px]">▾</span>}
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
