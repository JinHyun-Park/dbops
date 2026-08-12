// frontend/src/app/apm/page.tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchApmTargets,
  fetchApmOverview,
  searchApmLogs,
  type ApmTarget,
} from "@/lib/api-client";
import { useSmartPoll } from "@/lib/use-smart-poll";
import {
  PageBody,
  PageHeader,
  Section,
  Stat,
} from "@/components/design-system/page-shell";

const LEVELS = ["ERROR", "WARN", "INFO", "DEBUG"] as const;

interface LogEntry {
  ts: string;
  message: string;
}

export default function ApmPage() {
  const [targets, setTargets] = useState<ApmTarget[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [overview, setOverview] = useState<Record<string, unknown> | null>(
    null,
  );
  const [levels, setLevels] = useState<string[]>(["ERROR", "WARN"]);
  const [query, setQuery] = useState("");
  const [allLevels, setAllLevels] = useState(false);
  // Relative preset in minutes; 0 means "custom range" (use start/end below).
  const [rangeMin, setRangeMin] = useState<number>(60);
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchApmTargets()
      .then((r) => {
        setTargets(r.targets || []);
        if (!selected && r.targets?.length) setSelected(r.targets[0].target_id);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [selected]);

  const loadOverview = useCallback(() => {
    if (!selected) return;
    fetchApmOverview(selected)
      .then(setOverview)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [selected]);
  useSmartPoll(loadOverview, 15_000, [selected]);

  const runSearch = useCallback(() => {
    if (!selected) return;
    setSearching(true);
    setError(null);
    const opts: {
      levels?: string[];
      all?: boolean;
      query: string;
      limit: number;
      minutes?: number;
      start?: number;
      end?: number;
    } = { query, limit: 2000 };
    if (allLevels) opts.all = true;
    else opts.levels = levels;
    // The backend documents `levels: []` as "no filter, ALL levels" (see
    // _levels_filter in api/apm/handler.py), which is deliberate there. But in the UI
    // an empty list means the user unchecked every level, so sending it straight
    // through showed EVERYTHING when they asked for nothing. Block it here rather
    // than change the backend contract, which is documented and tested.
    if (!allLevels && levels.length === 0) {
      setSearching(false);
      setError("레벨을 최소 하나 선택하거나 '전체 레벨'을 켜세요.");
      return;
    }
    if (rangeMin === 0) {
      // Custom range: convert local datetime-local values to epoch seconds.
      if (customStart)
        opts.start = Math.floor(new Date(customStart).getTime() / 1000);
      if (customEnd)
        opts.end = Math.floor(new Date(customEnd).getTime() / 1000);
    } else {
      opts.minutes = rangeMin;
    }
    searchApmLogs(selected, opts)
      .then((r) => setLogs(r.entries || []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSearching(false));
  }, [selected, levels, allLevels, query, rangeMin, customStart, customEnd]);

  const toggleLevel = (lv: string) =>
    setLevels((cur) =>
      cur.includes(lv) ? cur.filter((x) => x !== lv) : [...cur, lv],
    );

  const metrics = (overview?.metrics as Record<string, number>) || {};
  const logCounts = (overview?.log_counts as Record<string, number>) || {};
  // Two-decimal display for percent/latency gauges.
  const fmt2 = (v: number) => v.toFixed(2);

  return (
    <PageBody>
      <PageHeader
        eyebrow="apm"
        title="APM"
        description="EC2 위 Java/Spring Boot 앱 로그·성능 모니터링. 지표는 캐시, 로그는 온디맨드 검색."
        actions={
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            className="text-xs bg-zinc-900 border border-zinc-700 rounded px-2 py-1.5"
          >
            {targets.length === 0 && <option value="">타겟 없음</option>}
            {targets.map((t) => (
              <option key={t.target_id} value={t.target_id}>
                {t.service_name || t.target_id}
              </option>
            ))}
          </select>
        }
      />

      {error && <div className="text-sm text-red-400">{error}</div>}

      {!selected ? (
        <Section title="APM 타겟 없음">
          <p className="text-sm text-zinc-400">
            먼저 APM 타겟(EC2 instance-id, region, spoke role, 로그 그룹)을
            등록하세요.
          </p>
        </Section>
      ) : (
        <>
          {/* Inline cards: individual bordered boxes, gap between them, no
              shared gray background (the design-system StatRow uses a zinc-800
              backing that shows through 1px gaps — we opt out of it here). */}
          <div className="flex flex-wrap gap-3">
            {/* latency/error come from Application Signals; show only when collected. */}
            {typeof metrics.latency_avg === "number" && (
              <div className="flex-1 min-w-40 border border-zinc-800 rounded-lg overflow-hidden">
                <Stat label="Latency avg" value={fmt2(metrics.latency_avg)} />
              </div>
            )}
            {typeof metrics.error_rate === "number" && (
              <div className="flex-1 min-w-40 border border-zinc-800 rounded-lg overflow-hidden">
                <Stat label="Error rate" value={fmt2(metrics.error_rate)} />
              </div>
            )}
            <div className="flex-1 min-w-40 border border-zinc-800 rounded-lg overflow-hidden">
              <Stat
                label="CPU %"
                value={
                  typeof metrics.cpu === "number" ? fmt2(metrics.cpu) : "—"
                }
              />
            </div>
            <div className="flex-1 min-w-40 border border-zinc-800 rounded-lg overflow-hidden">
              <Stat
                label="Mem %"
                value={
                  typeof metrics.mem === "number" ? fmt2(metrics.mem) : "—"
                }
              />
            </div>
            <div className="flex-1 min-w-40 border border-zinc-800 rounded-lg overflow-hidden">
              <Stat label="ERROR (1h)" value={logCounts.ERROR ?? 0} />
            </div>
            <div className="flex-1 min-w-40 border border-zinc-800 rounded-lg overflow-hidden">
              <Stat label="WARN (1h)" value={logCounts.WARN ?? 0} />
            </div>
          </div>

          <Section title="로그 검색">
            <div className="flex flex-wrap items-center gap-2 mb-3">
              {/* Time range presets + custom */}
              {[
                { label: "5분", m: 5 },
                { label: "10분", m: 10 },
                { label: "30분", m: 30 },
                { label: "1시간", m: 60 },
                { label: "6시간", m: 360 },
                { label: "사용자 지정", m: 0 },
              ].map((r) => (
                <button
                  key={r.label}
                  onClick={() => setRangeMin(r.m)}
                  className={`text-xs px-2 py-1 rounded border ${
                    rangeMin === r.m
                      ? "bg-emerald-900/40 border-emerald-500 text-emerald-300"
                      : "bg-zinc-900 border-zinc-700 text-zinc-500"
                  }`}
                >
                  {r.label}
                </button>
              ))}
              {rangeMin === 0 && (
                <>
                  <input
                    type="datetime-local"
                    value={customStart}
                    onChange={(e) => setCustomStart(e.target.value)}
                    className="text-xs bg-zinc-900 border border-zinc-700 rounded px-2 py-1"
                  />
                  <span className="text-xs text-zinc-500">~</span>
                  <input
                    type="datetime-local"
                    value={customEnd}
                    onChange={(e) => setCustomEnd(e.target.value)}
                    className="text-xs bg-zinc-900 border border-zinc-700 rounded px-2 py-1"
                  />
                </>
              )}
              {/* All-levels toggle */}
              <button
                onClick={() => setAllLevels((v) => !v)}
                className={`text-xs px-2 py-1 rounded border ${
                  allLevels
                    ? "bg-emerald-900/40 border-emerald-500 text-emerald-300"
                    : "bg-zinc-900 border-zinc-700 text-zinc-500"
                }`}
              >
                전체 레벨
              </button>
              {LEVELS.map((lv) => (
                <button
                  key={lv}
                  onClick={() => toggleLevel(lv)}
                  disabled={allLevels}
                  className={`text-xs px-2 py-1 rounded border ${
                    allLevels
                      ? "bg-zinc-900 border-zinc-800 text-zinc-600 cursor-not-allowed"
                      : levels.includes(lv)
                        ? "bg-zinc-700 border-zinc-500"
                        : "bg-zinc-900 border-zinc-700 text-zinc-500"
                  }`}
                >
                  {lv}
                </button>
              ))}
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="검색어 (선택)"
                className="text-xs bg-zinc-900 border border-zinc-700 rounded px-2 py-1 flex-1 min-w-40"
              />
              <button
                onClick={runSearch}
                disabled={searching}
                className="text-xs font-medium px-3 py-1.5 border border-zinc-700 rounded"
              >
                {searching ? "검색 중…" : "검색"}
              </button>
            </div>
            <div className="font-mono text-xs space-y-1 min-h-[55vh] max-h-[72vh] overflow-auto">
              {logs.length === 0 ? (
                <p className="text-zinc-500">
                  결과 없음. 레벨·검색어·타겟을 확인하세요.
                </p>
              ) : (
                logs.map((e, i) => (
                  <div key={i} className="border-b border-zinc-800 py-1">
                    <span className="text-zinc-500 mr-2">{e.ts}</span>
                    <span className="text-zinc-200 whitespace-pre-wrap">
                      {e.message}
                    </span>
                  </div>
                ))
              )}
            </div>
          </Section>
        </>
      )}
    </PageBody>
  );
}
