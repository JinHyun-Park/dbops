"use client";

import { useEffect, useState } from "react";
import { authedFetch, apiUrl, fetchClusterSettings } from "@/lib/api-client";

interface Setting {
  name: string;
  value: string;
  unit: string;
  updated_at: string;
}

interface ParamDiffRow {
  name: string;
  current: string;
  default: string | null;
  source: string;
  apply_type: string;
}

interface ParamDiffResponse {
  available: boolean;
  not_applicable?: boolean;
  diffs?: ParamDiffRow[];
  diff_count?: number;
}

type Rec = { value: string; why: string; severity: "warning" | "info" };

// Recommended values per setting. Mirrors the list in
// pg_health_checks.RECOMMENDED_SETTINGS — keep these in sync so the panel
// diff matches the Maintenance Health findings.
const PG_RECOMMENDED: Record<string, Rec> = {
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

// MySQL generic operational recommendations.
// Memory-sized settings (innodb_buffer_pool_size, *_buffer_size,
// max_connections, innodb_log_file_size, tmp_table_size, max_heap_table_size,
// thread_stack, innodb_io_capacity, innodb_read/write_io_threads) are
// intentionally absent — their correct values depend on the cluster's instance
// memory and workload profile, and are computed per-cluster by the Maintenance
// Health param-fitness panel. Adding static values here would be misleading.
const MYSQL_RECOMMENDED: Record<string, Rec> = {
  slow_query_log: {
    value: "ON",
    severity: "warning",
    why: "느린 쿼리 로깅을 켜야 성능 분석이 가능합니다.",
  },
  long_query_time: {
    value: "1",
    severity: "info",
    why: "1초 이상 쿼리를 느린 쿼리로 기록 — 워크로드에 따라 조정.",
  },
  innodb_flush_log_at_trx_commit: {
    value: "1",
    severity: "info",
    why: "1이 완전한 ACID 내구성 — 성능을 위해 2로 낮추는 건 트레이드오프.",
  },
  log_bin: {
    value: "ON",
    severity: "info",
    why: "바이너리 로그(PITR/복제) — Aurora는 클러스터에서 관리.",
  },
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

// ON/OFF settings where "1" and "ON" (or "0" and "OFF") are equivalent.
const ONOFF_SETTINGS = new Set([
  "slow_query_log",
  "log_bin",
  "log_checkpoints",
  "log_connections",
  "log_disconnections",
  "log_lock_waits",
]);

function matchesRecommendation(rec: Rec, name: string, value: string): boolean {
  if (ONOFF_SETTINGS.has(name)) {
    const norm = (v: string) => {
      const u = v.toUpperCase().trim();
      return u === "1" || u === "ON" ? "ON" : "OFF";
    };
    return norm(value) === norm(rec.value);
  }
  // PG: any positive value is acceptable for log_min_duration_statement.
  // MySQL: any positive value is acceptable for long_query_time.
  if (name === "log_min_duration_statement" || name === "long_query_time") {
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

  // 디폴트 대비 변경 — 별도 sub-view. 백엔드가 이미 diff만 반환하므로 별도
  // "전체/변경만" 토글은 불필요(rung 1: 존재할 필요 없는 기능은 생략) —
  // 접이식 블록 하나로 충분하다.
  const [diffOpen, setDiffOpen] = useState(true);
  const [diffLoading, setDiffLoading] = useState(true);
  const [diffAvailable, setDiffAvailable] = useState(false);
  const [diffs, setDiffs] = useState<ParamDiffRow[]>([]);

  useEffect(() => {
    let cancelled = false;
    setDiffLoading(true);
    (async () => {
      try {
        const res = await authedFetch(
          await apiUrl(
            `/api/dashboard/${encodeURIComponent(clusterId)}/param-diff`,
          ),
        );
        if (!res.ok) throw new Error(String(res.status));
        const d: ParamDiffResponse = await res.json();
        if (cancelled) return;
        setDiffAvailable(!!d.available);
        setDiffs(d.available ? d.diffs || [] : []);
      } catch {
        if (!cancelled) {
          setDiffAvailable(false);
          setDiffs([]);
        }
      } finally {
        if (!cancelled) setDiffLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [clusterId]);

  const engineLabel = (engine || "").includes("mysql")
    ? "MySQL"
    : (engine || "").includes("postgresql")
      ? "PostgreSQL"
      : "Engine";

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-5">
      <div className="text-sm text-zinc-200 font-medium mb-1">
        {engineLabel} Configuration
      </div>
      {/* 반영 지연 안내 — 파라미터 변경 승인 후 "바로 안 바뀐다"는 혼란을
          막는다: 값은 5분 주기 수집 캐시이고, 변경은 pending-reboot라 재시작
          전까지 동작값이 바뀌지 않는다. */}
      <div className="text-[11px] text-zinc-500 mb-3">
        설정값은 5분 주기로 수집됩니다. 파라미터 변경은 pending-reboot로
        적용되어 <span className="text-amber-300/90">인스턴스 재시작 후</span>{" "}
        동작값에 반영됩니다.
      </div>
      {loading ? (
        <div className="text-zinc-500 text-sm">불러오는 중…</div>
      ) : settings.length === 0 ? (
        <div className="text-zinc-500 text-sm">수집된 설정이 없습니다</div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          {settings.map((s) => {
            const isPg = engineLabel === "PostgreSQL";
            const isMy = engineLabel === "MySQL";
            const rec = isPg
              ? PG_RECOMMENDED[s.name]
              : isMy
                ? MYSQL_RECOMMENDED[s.name]
                : undefined;
            const ok = !rec || matchesRecommendation(rec, s.name, s.value);
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

      <div className="mt-4 pt-3 border-t border-zinc-800">
        <button
          type="button"
          onClick={() => setDiffOpen((o) => !o)}
          className="text-sm text-zinc-200 font-medium flex items-center gap-1.5"
        >
          <span className="text-zinc-500">{diffOpen ? "▾" : "▸"}</span>
          디폴트 대비 변경{diffAvailable ? ` (${diffs.length})` : ""}
        </button>
        {diffOpen && (
          <div className="mt-2">
            {diffLoading ? (
              <div className="text-zinc-500 text-sm">불러오는 중…</div>
            ) : !diffAvailable ? (
              <div className="text-zinc-500 text-sm">디폴트 비교 미조회</div>
            ) : diffs.length === 0 ? (
              <div className="text-zinc-500 text-sm">디폴트와 동일합니다</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-[11px] text-zinc-500 text-left">
                      <th className="font-normal pb-1.5 pr-3">파라미터</th>
                      <th className="font-normal pb-1.5 pr-3">현재값</th>
                      <th className="font-normal pb-1.5 pr-3">디폴트값</th>
                      <th className="font-normal pb-1.5">적용</th>
                    </tr>
                  </thead>
                  <tbody>
                    {diffs.map((d) => (
                      <tr key={d.name} className="border-t border-zinc-800/60">
                        <td className="py-1.5 pr-3 font-mono text-zinc-300 text-xs">
                          {d.name}
                        </td>
                        <td className="py-1.5 pr-3 font-mono text-zinc-100 text-xs">
                          {d.current}
                        </td>
                        <td className="py-1.5 pr-3 font-mono text-zinc-500 text-xs">
                          {d.default ?? "—"}
                        </td>
                        <td className="py-1.5">
                          <span
                            className={`text-[9px] uppercase tracking-wider px-1 py-0.5 rounded border ${
                              d.apply_type === "static"
                                ? "border-amber-500/40 text-amber-300"
                                : "border-zinc-700 text-zinc-400"
                            }`}
                          >
                            {d.apply_type || "unknown"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
