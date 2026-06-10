"use client";

import { useEffect, useState } from "react";
import { fetchClusterSettings } from "@/lib/api-client";

interface Setting {
  name: string;
  value: string;
  unit: string;
  updated_at: string;
}

// Recommended values per setting. Mirrors the list in
// pg_health_checks.RECOMMENDED_SETTINGS — keep these in sync so the panel
// diff matches the Maintenance Health findings.
const PG_RECOMMENDED: Record<
  string,
  { value: string; why: string; severity: "warning" | "info" }
> = {
  log_checkpoints: {
    value: "on",
    severity: "warning",
    why: "체크포인트 타이밍 분석에 필요합니다.",
  },
  log_connections: {
    value: "on",
    severity: "info",
    why: "pgBadger 세션 리포트에 필요합니다.",
  },
  log_disconnections: {
    value: "on",
    severity: "info",
    why: "pgBadger 세션 리포트에 필요합니다.",
  },
  log_lock_waits: {
    value: "on",
    severity: "warning",
    why: "락 경합 진단이 이 설정에 의존합니다.",
  },
  log_autovacuum_min_duration: {
    value: "0",
    severity: "warning",
    why: "0이면 모든 autovacuum을 로깅 — pgBadger가 bloat와 상관분석합니다.",
  },
  log_min_duration_statement: {
    value: "1000",
    severity: "warning",
    why: "1초 미만 쿼리는 로깅 제외가 적절 — 1000ms가 합리적인 하한입니다.",
  },
  log_temp_files: {
    value: "0",
    severity: "info",
    why: "디스크로 스필하는 쿼리를 잡아냅니다.",
  },
};

// Postgres uses negative sentinels for "disabled" / "auto" on several settings.
// Rendering them through the unit conversion produces nonsense like "-0.0 MB"
// or "-1ms", so map them to readable labels before the formatter runs.
const NEGATIVE_SENTINEL: Record<string, string> = {
  log_min_duration_statement: "disabled",
  log_autovacuum_min_duration: "disabled",
  log_temp_files: "off",
  log_duration: "off",
  log_parser_stats: "off",
  log_planner_stats: "off",
  log_executor_stats: "off",
  log_statement_stats: "off",
  // wal_buffers default is -1 → auto-sized from shared_buffers/32
  wal_buffers: "auto",
  effective_io_concurrency: "default",
  vacuum_cost_delay: "default",
};

function fmtValue(s: Setting): string {
  const v = s.value;
  const u = s.unit;
  const num = Number(v);
  // Sentinel handling — -1 has setting-specific semantics in PG.
  if (Number.isFinite(num) && num < 0 && NEGATIVE_SENTINEL[s.name]) {
    return `${NEGATIVE_SENTINEL[s.name]} (${v})`;
  }
  if (!u) return v;
  if (!Number.isFinite(num)) return `${v} ${u}`;
  // unit conversions
  if (u === "8kB") return `${((num * 8) / 1024).toFixed(1)} MB`;
  if (u === "kB") return `${(num / 1024).toFixed(1)} MB`;
  if (u === "s") return `${num}s`;
  if (u === "ms") return `${num}ms`;
  return `${v} ${u}`;
}

function matchesRecommendation(name: string, value: string): boolean {
  const rec = PG_RECOMMENDED[name];
  if (!rec) return true; // not a recommendation we track — treat as OK
  if (name === "log_min_duration_statement") {
    // any positive integer is acceptable
    const iv = Number(value);
    return Number.isFinite(iv) && iv > 0;
  }
  return value === rec.value;
}

export function SettingsPanel({
  clusterId,
  engine,
}: {
  clusterId: string;
  engine?: string;
}) {
  const [settings, setSettings] = useState<Setting[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchClusterSettings(clusterId)
      .then((d) => !cancelled && setSettings(d.settings || []))
      .catch(() => !cancelled && setSettings([]))
      .finally(() => !cancelled && setLoading(false));
  }, [clusterId]);

  const engineLabel = (engine || "").includes("mysql")
    ? "MySQL"
    : (engine || "").includes("postgresql")
      ? "PostgreSQL"
      : "Engine";

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-5">
      <div className="text-sm text-zinc-200 font-medium mb-3">
        {engineLabel} Configuration
      </div>
      {loading ? (
        <div className="text-zinc-500 text-sm">불러오는 중…</div>
      ) : settings.length === 0 ? (
        <div className="text-zinc-500 text-sm">수집된 설정이 없습니다</div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          {settings.map((s) => {
            const isPg = engineLabel === "PostgreSQL";
            const rec = isPg ? PG_RECOMMENDED[s.name] : undefined;
            const ok = !rec || matchesRecommendation(s.name, s.value);
            const borderClass = !rec
              ? "border-zinc-800"
              : ok
                ? "border-emerald-500/20"
                : rec.severity === "warning"
                  ? "border-amber-500/40"
                  : "border-sky-500/30";
            return (
              <div
                key={s.name}
                className={`bg-zinc-950 border ${borderClass} rounded p-3`}
                title={rec ? rec.why : undefined}
              >
                <div className="flex items-center justify-between mb-1 gap-1.5">
                  <div
                    className="text-[11px] text-zinc-500 font-mono truncate"
                    title={s.name}
                  >
                    {s.name}
                  </div>
                  {rec && (
                    <span
                      className={`text-[9px] uppercase tracking-wider px-1 py-0.5 rounded ${
                        ok
                          ? "border border-emerald-500/40 text-emerald-300"
                          : rec.severity === "warning"
                            ? "border border-amber-500/40 text-amber-300"
                            : "border border-sky-500/30 text-sky-300"
                      }`}
                    >
                      {ok ? "✓" : "⚠"}
                    </span>
                  )}
                </div>
                <div className="text-sm text-zinc-100 font-mono">
                  {fmtValue(s)}
                </div>
                {rec && !ok && (
                  <div className="text-[10px] text-zinc-500 mt-1">
                    recommended:{" "}
                    <span className="text-amber-300 font-mono">
                      {rec.value}
                    </span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
