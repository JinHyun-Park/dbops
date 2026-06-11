"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchHealthFindings, type HealthFinding } from "@/lib/api-client";
import { streamChat } from "@/lib/agentcore-sse";
import { fmtRelative } from "@/lib/format";

const SEV_BADGE: Record<HealthFinding["severity"], string> = {
  critical: "bg-rose-500/20 text-rose-300 border border-rose-500/40",
  warning: "bg-amber-500/15 text-amber-300 border border-amber-500/40",
  info: "bg-sky-500/15 text-sky-300 border border-sky-500/30",
};

const SEV_DOT: Record<HealthFinding["severity"], string> = {
  critical: "bg-rose-400",
  warning: "bg-amber-400",
  info: "bg-sky-400",
};

// Display labels for check_type so the filter tabs read like operational
// categories instead of snake_case internals.
const CHECK_LABELS: Record<string, string> = {
  txid_age: "VACUUM",
  dead_tuples: "VACUUM",
  vacuum_overdue: "VACUUM",
  table_bloat: "Bloat",
  index_unused: "Indexes",
  extension_missing: "Extensions",
  setting_misconfigured: "Config",
  cost_oversized: "Cost",
  cost_serverless_max_too_high: "Cost",
  cost_serverless_min_too_low: "Cost",
  cost_savings_plan_opportunity: "Cost",
  // Parameter Fitness — 이 클러스터 워크로드 기준 파라미터 적정성 진단.
  param_max_connections: "Tuning",
  param_work_mem_risk: "Tuning", // PG
  param_effective_cache: "Tuning", // PG
  param_autovacuum_workers: "Tuning", // PG
  param_buffer_cache_hit: "Tuning",
  param_mysql_conn_buffers: "Tuning", // MySQL: per-connection 버퍼 × max_conn OOM 위험
  // 고갈 예측 경보 — storage/connection/ACU 한계 도달 ETA.
  capacity_forecast: "Capacity",
  // DynamoDB findings — ddb_* check_types from dynamodb_findings collector.
  ddb_throttling: "Throttling",
  ddb_capacity_underprovisioned: "Capacity",
  ddb_capacity_overprovisioned: "Capacity",
  ddb_hot_partition: "Hot Partition",
  ddb_ondemand_high_throughput: "Cost",
};

// Full PG tab set. MySQL exposes a trimmed list (VACUUM/Bloat/Extensions are
// PG-only collectors today; Indexes/Config are PG-leaning but kept for
// forward-compatibility once MySQL parity ships).
const TABS_PG = [
  "All",
  "VACUUM",
  "Bloat",
  "Indexes",
  "Config",
  "Tuning",
  "Capacity",
  "Extensions",
  "Cost",
] as const;
// MySQL now has Parameter Fitness (mysql_param_fitness) and capacity/ACU
// forecast (capacity_forecast is engine-agnostic) — so Tuning/Capacity apply.
// VACUUM/Bloat/Indexes/Config/Extensions remain PG-only collectors.
const TABS_MYSQL = ["All", "Tuning", "Capacity", "Cost"] as const;
// DynamoDB findings cover throttling, capacity sizing, hot-partition detection,
// and on-demand cost signals — each gets its own filter tab.
const TABS_DYNAMODB = [
  "All",
  "Throttling",
  "Capacity",
  "Hot Partition",
  "Cost",
] as const;
type Tab =
  | (typeof TABS_PG)[number]
  | (typeof TABS_MYSQL)[number]
  | (typeof TABS_DYNAMODB)[number];

function tryParse(
  raw: HealthFinding["details"],
): Record<string, unknown> | null {
  if (raw == null) return null;
  if (typeof raw === "object") return raw as Record<string, unknown>;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function MaintenanceHealthPanel({
  clusterId,
  engine,
}: {
  clusterId: string;
  engine?: string;
}) {
  const [findings, setFindings] = useState<HealthFinding[]>([]);
  const [counts, setCounts] = useState({ critical: 0, warning: 0, info: 0 });
  const [snapshotTime, setSnapshotTime] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>("All");
  const [active, setActive] = useState<HealthFinding | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchHealthFindings(clusterId)
        .then((d) => {
          if (cancelled) return;
          setFindings(d.findings || []);
          setCounts(d.counts || { critical: 0, warning: 0, info: 0 });
          setSnapshotTime(d.snapshot_time);
        })
        .catch(() => {
          if (cancelled) return;
          setFindings([]);
        })
        .finally(() => !cancelled && setLoading(false));
    load();
    const iv = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, [clusterId]);

  const filtered = useMemo(() => {
    if (tab === "All") return findings;
    return findings.filter((f) => CHECK_LABELS[f.check_type] === tab);
  }, [findings, tab]);

  // If a tab is selected that doesn't exist for the current engine family,
  // snap back to "All" so the user isn't stuck on a missing tab.
  useEffect(() => {
    const e = (engine || "").toLowerCase();
    const allowed: string[] = (e.includes("postgres")
      ? TABS_PG
      : e.includes("dynamodb")
        ? TABS_DYNAMODB
        : TABS_MYSQL) as unknown as string[];
    if (!allowed.includes(tab)) setTab("All");
  }, [engine, tab]);

  // Select the correct tab strip per engine family.
  const e = (engine || "").toLowerCase();
  const isPg = e.includes("postgres");
  const isDynamoDB = e.includes("dynamodb");
  const tabs: readonly Tab[] = isPg
    ? TABS_PG
    : isDynamoDB
      ? TABS_DYNAMODB
      : TABS_MYSQL;

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800">
        <div className="flex items-baseline justify-between gap-3 flex-wrap">
          <div>
            <div className="text-sm text-zinc-200 font-medium">
              Maintenance Health
            </div>
            <div className="text-[11px] text-zinc-500 mt-0.5">
              DBA가 조치할 항목을 심각도 순으로 정렬했어요. 행을 클릭하면 AI가
              조치를 제안합니다.
              {snapshotTime && (
                <span className="ml-2 text-zinc-600">
                  · {fmtRelative(snapshotTime)} 갱신
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2 text-[11px]">
            <span
              className={`px-1.5 py-0.5 rounded font-mono ${SEV_BADGE.critical}`}
            >
              🔴 {counts.critical} critical
            </span>
            <span
              className={`px-1.5 py-0.5 rounded font-mono ${SEV_BADGE.warning}`}
            >
              🟡 {counts.warning} warning
            </span>
            <span
              className={`px-1.5 py-0.5 rounded font-mono ${SEV_BADGE.info}`}
            >
              ℹ {counts.info} info
            </span>
          </div>
        </div>
        <div className="flex items-center gap-1 mt-3">
          {tabs.map((t) => {
            const isActive = tab === t;
            return (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`text-[10px] uppercase tracking-wider px-2 py-1 border transition-colors ${
                  isActive
                    ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
                    : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
                }`}
              >
                {t}
              </button>
            );
          })}
        </div>
      </div>

      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">불러오는 중…</div>
      ) : filtered.length === 0 ? (
        <div className="p-6 text-emerald-400 text-sm flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-500" />
          {tab === "All"
            ? "발견된 이슈가 없어요 — 클러스터 상태 양호 🎉"
            : `${tab} 카테고리에 해당하는 항목이 없어요`}
        </div>
      ) : (
        <div className="max-h-[28rem] overflow-y-auto divide-y divide-zinc-800">
          {filtered.map((f) => (
            <button
              key={f.id}
              onClick={() => setActive(f)}
              className="w-full text-left px-4 py-2.5 hover:bg-zinc-800/40 transition-colors"
            >
              <div className="flex items-start gap-2.5">
                <span
                  className={`w-2 h-2 rounded-full ${
                    SEV_DOT[f.severity]
                  } mt-1.5 flex-shrink-0`}
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span
                      className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${
                        SEV_BADGE[f.severity]
                      }`}
                    >
                      {f.severity}
                    </span>
                    <span className="text-[10px] uppercase tracking-wider text-zinc-500 font-mono">
                      {f.check_type}
                    </span>
                    <span className="text-sm text-zinc-200 font-mono truncate">
                      {f.subject}
                    </span>
                  </div>
                  <div className="text-xs text-zinc-400 mt-1">
                    <span className="text-zinc-200">{f.value_str}</span>
                    <span className="text-zinc-600"> · target </span>
                    <span className="font-mono">{f.threshold_str}</span>
                  </div>
                  <div className="text-xs text-zinc-300 mt-1 leading-snug">
                    {f.recommendation}
                  </div>
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      {active && (
        <FindingDetailModal
          finding={active}
          clusterId={clusterId}
          onClose={() => setActive(null)}
        />
      )}
    </div>
  );
}

function FindingDetailModal({
  finding,
  clusterId,
  onClose,
}: {
  finding: HealthFinding;
  clusterId: string;
  onClose: () => void;
}) {
  const [insight, setInsight] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

  const details = tryParse(finding.details);

  // 사용자가 명시적으로 조치를 진행할 때만 chat으로 — 거기서 에이전트가
  // request_approval을 호출해 승인 센터에 올린다. "원인+조치"(설명)와
  // "승인 요청 생성"을 분리해, 단순 확인이 승인 센터를 오염시키지 않게 한다.
  const proceedInChat = () => {
    const prompt =
      `${finding.subject} (${finding.check_type}) 항목을 조치하고 싶어. ` +
      `현재값 ${finding.value_str}, 권장 ${finding.threshold_str}. ` +
      `조치를 실행하기 위한 승인 요청을 만들어줘.`;
    router.push(
      `/chat?cluster=${encodeURIComponent(
        clusterId,
      )}&prompt=${encodeURIComponent(prompt)}`,
    );
  };

  const handleAnalyze = () => {
    setInsight("");
    setError(null);
    setLoading(true);
    const detailJson = JSON.stringify(details ?? {}, null, 2);
    const message =
      `너는 시니어 PostgreSQL DBA야. 아래 유지보수 항목을 **한국어로** 다음 3개 섹션으로 짧고 명확하게 설명해줘:\n` +
      `1. **왜 중요한지** — 운영 리스크 한 문장.\n` +
      `2. **구체적 조치** — 실행해야 할 정확한 명령어 또는 파라미터 변경. schema.table 이름까지 포함해.\n` +
      `3. **검증 방법** — 조치가 반영됐는지 확인할 쿼리나 점검 한 가지.\n\n` +
      // 중요: 이 호출은 "설명만" 받는 읽기 전용이다. 도구를 호출하면
      // 에이전트가 request_approval을 자동 실행해 승인 센터에 항목이
      // 쌓인다(사용자는 확인만 하려던 것). 실제 승인 요청은 사용자가
      // 아래 'Chat에서 조치 진행' 버튼으로 명시적으로 시작한다.
      `**중요: 절대 어떤 도구도 호출하지 마. request_approval·execute_sql 등 쓰기/승인 도구를 호출하지 말고, 위 3개 섹션 설명만 텍스트로 제공해. 실제 실행은 사용자가 별도로 진행한다.**\n\n` +
      `Cluster: ${clusterId}\n` +
      `Check: ${finding.check_type} (${finding.severity})\n` +
      `Subject: ${finding.subject}\n` +
      `Observed: ${finding.value_str}\n` +
      `Threshold: ${finding.threshold_str}\n` +
      `Initial recommendation: ${finding.recommendation}\n\n` +
      `Extra context:\n\`\`\`json\n${detailJson}\n\`\`\``;
    streamChat(
      message,
      clusterId,
      (t) => setInsight((p) => p + t),
      () => {},
      () => setLoading(false),
      (err) => {
        setError(err.message);
        setLoading(false);
      },
    );
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-zinc-950/80 backdrop-blur flex items-center justify-center p-5"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl max-h-[90vh] flex flex-col bg-zinc-900 border border-zinc-700 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between px-5 py-4 border-b border-zinc-800">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <span
                className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${
                  SEV_BADGE[finding.severity]
                }`}
              >
                {finding.severity}
              </span>
              <span className="text-[10px] text-zinc-500 font-mono">
                {finding.check_type}
              </span>
            </div>
            <h2 className="text-lg font-semibold text-zinc-100 font-mono truncate">
              {finding.subject}
            </h2>
            <div className="text-xs text-zinc-400 mt-1">
              <span className="text-zinc-200">{finding.value_str}</span>
              <span className="text-zinc-600"> · target </span>
              <span className="font-mono">{finding.threshold_str}</span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-200 text-xl leading-none ml-3"
            aria-label="닫기"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          <div className="mb-4">
            <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 mb-1">
              초기 권장 조치
            </div>
            <div className="text-sm text-zinc-200">
              {finding.recommendation}
            </div>
          </div>

          {details && Object.keys(details).length > 0 && (
            <div className="mb-4">
              <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 mb-1">
                상세 컨텍스트
              </div>
              <pre className="text-[11px] font-mono text-zinc-400 bg-zinc-950 border border-zinc-800 px-3 py-2 overflow-auto">
                {JSON.stringify(details, null, 2)}
              </pre>
            </div>
          )}

          <div className="border-t border-zinc-800 pt-3">
            <div className="flex items-center justify-between mb-2">
              <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
                AI 조치 제안
              </div>
              <button
                onClick={handleAnalyze}
                disabled={loading}
                className="text-xs px-3 py-1 border border-sky-500/40 text-sky-300 hover:bg-sky-500/10 disabled:opacity-50 transition-colors"
              >
                {loading ? "분석 중…" : insight ? "다시 분석" : "원인 + 조치"}
              </button>
            </div>
            {error && (
              <div className="text-xs text-rose-400 border border-rose-500/40 bg-rose-500/10 px-3 py-2 mb-2">
                {error}
              </div>
            )}
            {!insight && !loading && !error && (
              <div className="text-xs text-zinc-500">
                <span className="text-sky-300">원인 + 조치</span> 버튼을 누르면
                리스크 설명 + 정확한 명령어 + 검증 방법을 받아볼 수 있어요.
              </div>
            )}
            {insight && (
              <>
                <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-zinc-950 prose-pre:border prose-pre:border-zinc-800 prose-code:text-sky-300">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {insight}
                  </ReactMarkdown>
                </div>
                {/* 설명을 본 뒤, 실제로 조치를 올리고 싶을 때만 명시적으로
                    승인 흐름으로. 이 버튼을 눌러야 승인 센터에 항목이 생긴다. */}
                <div className="mt-3 pt-3 border-t border-zinc-800 flex items-center justify-between gap-3">
                  <span className="text-[11px] text-zinc-500">
                    설명만 확인했다면 닫아도 됩니다. 실제 조치가 필요하면:
                  </span>
                  <button
                    onClick={proceedInChat}
                    className="text-xs px-3 py-1.5 border border-amber-500/50 text-amber-300 hover:bg-amber-500/10 transition-colors whitespace-nowrap"
                  >
                    Chat에서 조치 진행 →
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
