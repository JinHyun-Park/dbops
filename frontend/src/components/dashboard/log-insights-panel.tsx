"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchLogInsights,
  type LogCategory,
  type LogInsightsResponse,
} from "@/lib/api-client";

const CATEGORIES: { key: LogCategory; label: string; hint: string }[] = [
  { key: "all", label: "All", hint: "기간 내 모든 로그 라인" },
  {
    key: "slow",
    label: "Slow Queries",
    hint: "log_min_duration_statement에 잡힌 라인",
  },
  {
    key: "vacuum",
    label: "Autovacuum",
    hint: "automatic vacuum / analyze 라인",
  },
  { key: "error", label: "Errors", hint: "ERROR / FATAL / PANIC 라인" },
  {
    key: "connection",
    label: "Connections",
    hint: "connection received / authorized / disconnection",
  },
];

// Highlight the first error/fatal/panic token in red so the eye lands on
// severity before the message body. Same recipe Datadog DBM uses on its
// log panel.
function highlightSeverity(message: string): React.ReactNode {
  const m = message.match(/\b(ERROR|FATAL|PANIC|WARNING|LOG|NOTICE):/);
  if (!m) return message;
  const before = message.slice(0, m.index ?? 0);
  const sev = m[0];
  const after = message.slice((m.index ?? 0) + sev.length);
  const cls =
    sev === "ERROR:" || sev === "FATAL:" || sev === "PANIC:"
      ? "text-rose-300 font-medium"
      : sev === "WARNING:"
        ? "text-amber-300 font-medium"
        : "text-zinc-500";
  return (
    <>
      {before}
      <span className={cls}>{sev}</span>
      {after}
    </>
  );
}

function relTime(iso?: string): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60_000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  return `${Math.floor(h / 24)}일 전`;
}

export function LogInsightsPanel({ clusterId }: { clusterId: string }) {
  const [category, setCategory] = useState<LogCategory>("all");
  const [hours, setHours] = useState<number>(1);
  const [data, setData] = useState<LogInsightsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState<number | null>(null);
  // We deliberately don't auto-load on mount — CW Logs Insights is billed
  // per GB scanned, so the first query is gated behind an explicit click.
  // Once the user opens a category we cache that category's result.

  const load = useCallback(
    async (cat: LogCategory, h: number) => {
      setLoading(true);
      try {
        const res = await fetchLogInsights(clusterId, cat, h);
        setData(res);
        setRefreshedAt(Date.now());
      } catch (e) {
        setData({
          cluster_id: clusterId,
          category: cat,
          hours: h,
          log_group: "",
          count: 0,
          entries: [],
          error: e instanceof Error ? e.message : "fetch failed",
        });
        setRefreshedAt(Date.now());
      } finally {
        setLoading(false);
      }
    },
    [clusterId],
  );

  // Reset when cluster changes — old cluster's logs aren't relevant.
  useEffect(() => {
    setData(null);
    setRefreshedAt(null);
  }, [clusterId]);

  const cwConsoleUrl = data?.log_group
    ? `https://console.aws.amazon.com/cloudwatch/home?region=ap-northeast-2#logsV2:log-groups/log-group/${encodeURIComponent(
        data.log_group,
      )}`
    : null;

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <div className="text-xs text-zinc-400 uppercase tracking-wider">
            Log Insights
            {data && !data.error && (
              <span className="ml-2 px-1.5 py-0.5 bg-sky-500/15 text-sky-300 border border-sky-500/30 text-[10px]">
                {data.count}
              </span>
            )}
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            CloudWatch Logs Insights에서 카테고리별로 PostgreSQL 로그 라인을
            가져옵니다. CW 스캔 비용이 발생하므로 자동 새로고침은 없습니다.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-xs rounded px-2 py-1"
          >
            <option value={1}>last 1h</option>
            <option value={6}>last 6h</option>
            <option value={24}>last 24h</option>
          </select>
          <button
            onClick={() => load(category, hours)}
            disabled={loading}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors"
          >
            {loading ? "검색 중…" : data ? "새로고침" : "로그 가져오기"}
          </button>
        </div>
      </div>

      <div className="flex items-center gap-1 px-4 py-2 border-b border-zinc-800 flex-wrap">
        {CATEGORIES.map((c) => (
          <button
            key={c.key}
            onClick={() => {
              setCategory(c.key);
              load(c.key, hours);
            }}
            title={c.hint}
            className={`text-[10px] uppercase tracking-wider px-2 py-1 border transition-colors ${
              category === c.key
                ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
                : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {c.label}
          </button>
        ))}
        {refreshedAt && (
          <span className="ml-auto text-[10px] text-zinc-600">
            {relTime(new Date(refreshedAt).toISOString())} 갱신
          </span>
        )}
        {cwConsoleUrl && (
          <a
            href={cwConsoleUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[10px] text-sky-400 hover:text-sky-300"
            title="CloudWatch 콘솔에서 원본 로그 보기"
          >
            CW 콘솔 열기 →
          </a>
        )}
      </div>

      <div className="max-h-96 overflow-y-auto">
        {!data && !loading && (
          <div className="p-6 text-zinc-500 text-sm">
            <span className="text-amber-300">로그 가져오기</span> 버튼을 눌러
            카테고리를 선택하세요. 첫 호출은 CW Logs Insights 쿼리를 시작하며
            5~10초 정도 걸립니다.
          </div>
        )}
        {loading && (
          <div className="p-6 text-zinc-500 text-sm">불러오는 중…</div>
        )}
        {data?.error && (
          <div className="p-4">
            <div className="text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
              {data.error}
            </div>
          </div>
        )}
        {data && !data.error && data.entries.length === 0 && (
          <div className="p-6 text-emerald-400 text-sm flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-emerald-500" />
            해당 카테고리에 매칭되는 로그 라인 없음 ({hours}h 기준)
          </div>
        )}
        {data && !data.error && data.entries.length > 0 && (
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/60 border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-500 sticky top-0">
              <tr>
                <th className="text-left px-3 py-2 font-medium w-40">When</th>
                <th className="text-left px-3 py-2 font-medium">Message</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800">
              {data.entries.map((entry, i) => (
                <tr
                  key={`${entry.ts}-${i}`}
                  className="hover:bg-zinc-900/40 align-top"
                >
                  <td className="px-3 py-2 text-zinc-400 font-mono text-[11px] whitespace-nowrap">
                    {entry.ts ? new Date(entry.ts).toLocaleString() : "—"}
                  </td>
                  <td className="px-3 py-2 text-zinc-200 font-mono text-[11px] break-all">
                    {highlightSeverity(entry.message)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
