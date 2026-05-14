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
    why: "Required for checkpoint timing analysis.",
  },
  log_connections: {
    value: "on",
    severity: "info",
    why: "pgBadger session report needs this.",
  },
  log_disconnections: {
    value: "on",
    severity: "info",
    why: "pgBadger session report needs this.",
  },
  log_lock_waits: {
    value: "on",
    severity: "warning",
    why: "Lock contention diagnosis depends on this.",
  },
  log_autovacuum_min_duration: {
    value: "0",
    severity: "warning",
    why: "0 logs every autovacuum — pgBadger correlates with bloat.",
  },
  log_min_duration_statement: {
    value: "1000",
    severity: "warning",
    why: "Below 1s queries shouldn't log; 1000ms is a reasonable floor.",
  },
  log_temp_files: {
    value: "0",
    severity: "info",
    why: "Catches queries that spill to disk.",
  },
};

function fmtValue(s: Setting): string {
  const v = s.value;
  const u = s.unit;
  if (!u) return v;
  const num = Number(v);
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
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">
        {engineLabel} Configuration
      </div>
      {loading ? (
        <div className="text-zinc-500 text-sm">Loading...</div>
      ) : settings.length === 0 ? (
        <div className="text-zinc-500 text-sm">no settings collected</div>
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
