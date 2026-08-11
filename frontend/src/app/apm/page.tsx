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
  StatRow,
} from "@/components/design-system/page-shell";

const LEVELS = ["ERROR", "WARN", "INFO", "DEBUG"] as const;

interface LogEntry {
  ts: string;
  message: string;
}

export default function ApmPage() {
  const [targets, setTargets] = useState<ApmTarget[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [overview, setOverview] = useState<Record<string, unknown> | null>(null);
  const [levels, setLevels] = useState<string[]>(["ERROR", "WARN"]);
  const [query, setQuery] = useState("");
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
    searchApmLogs(selected, { levels, query, hours: 1, limit: 100 })
      .then((r) => setLogs(r.entries || []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSearching(false));
  }, [selected, levels, query]);

  const toggleLevel = (lv: string) =>
    setLevels((cur) => (cur.includes(lv) ? cur.filter((x) => x !== lv) : [...cur, lv]));

  const metrics = (overview?.metrics as Record<string, number>) || {};
  const logCounts = (overview?.log_counts as Record<string, number>) || {};

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
            먼저 APM 타겟(EC2 instance-id, region, spoke role, 로그 그룹)을 등록하세요.
          </p>
        </Section>
      ) : (
        <>
          <StatRow>
            <Stat label="Latency p99" value={metrics.latency_p99 ?? "—"} />
            <Stat label="Error rate" value={metrics.error_rate ?? "—"} />
            <Stat label="CPU %" value={metrics.cpu ?? "—"} />
            <Stat label="Mem %" value={metrics.mem ?? "—"} />
          </StatRow>
          <StatRow>
            <Stat label="ERROR (1h)" value={logCounts.ERROR ?? 0} />
            <Stat label="WARN (1h)" value={logCounts.WARN ?? 0} />
          </StatRow>

          <Section title="로그 검색">
            <div className="flex flex-wrap items-center gap-2 mb-3">
              {LEVELS.map((lv) => (
                <button
                  key={lv}
                  onClick={() => toggleLevel(lv)}
                  className={`text-xs px-2 py-1 rounded border ${
                    levels.includes(lv)
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
            <div className="font-mono text-xs space-y-1 max-h-96 overflow-auto">
              {logs.length === 0 ? (
                <p className="text-zinc-500">결과 없음. 레벨·검색어·타겟을 확인하세요.</p>
              ) : (
                logs.map((e, i) => (
                  <div key={i} className="border-b border-zinc-800 py-1">
                    <span className="text-zinc-500 mr-2">{e.ts}</span>
                    <span className="text-zinc-200 whitespace-pre-wrap">{e.message}</span>
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
