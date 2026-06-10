"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchSlo,
  type SloDayBucket,
  type SloResponse,
} from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";
import { fmtDecimal, fmtExact } from "@/lib/format";
import { isMysql } from "@/lib/engine";
import { useSelectedCluster } from "@/lib/use-selected-cluster";
import { ClusterPicker } from "@/components/design-system/cluster-picker";

// Per-cluster target config persisted to localStorage. We keep this client-
// side for v1 — there is no team-level "official" SLO yet, just a personal
// dial. Migration path: write the same shape into a future DDB table.
interface SloConfig {
  availability_target_pct: number;
  latency_target_ms: number;
  days: number;
}

const DEFAULT_CONFIG: SloConfig = {
  availability_target_pct: 99.9,
  latency_target_ms: 100,
  days: 30,
};

const LS_KEY = (clusterId: string) => `dbops:slo:${clusterId}`;

function loadConfig(clusterId: string): SloConfig {
  if (typeof window === "undefined") return DEFAULT_CONFIG;
  try {
    const raw = window.localStorage.getItem(LS_KEY(clusterId));
    if (!raw) return DEFAULT_CONFIG;
    const parsed = JSON.parse(raw) as Partial<SloConfig>;
    return {
      availability_target_pct:
        parsed.availability_target_pct ??
        DEFAULT_CONFIG.availability_target_pct,
      latency_target_ms:
        parsed.latency_target_ms ?? DEFAULT_CONFIG.latency_target_ms,
      days: parsed.days ?? DEFAULT_CONFIG.days,
    };
  } catch {
    return DEFAULT_CONFIG;
  }
}

function saveConfig(clusterId: string, cfg: SloConfig) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LS_KEY(clusterId), JSON.stringify(cfg));
  } catch {
    // localStorage may be unavailable (private mode) — silently no-op
  }
}

export default function SloPage() {
  const { clusters, selected: selectedCluster } = useSelectedCluster();
  const engine = clusters.find((c) => c.cluster_id === selectedCluster)?.engine;
  const [config, setConfig] = useState<SloConfig>(DEFAULT_CONFIG);
  const [data, setData] = useState<SloResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Reload persisted config whenever the cluster changes.
  useEffect(() => {
    if (selectedCluster) setConfig(loadConfig(selectedCluster));
  }, [selectedCluster]);

  const load = useCallback(async () => {
    if (!selectedCluster) return;
    setLoading(true);
    setErr(null);
    try {
      const r = await fetchSlo(
        selectedCluster,
        config.days,
        config.availability_target_pct,
        config.latency_target_ms,
      );
      setData(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }, [
    selectedCluster,
    config.days,
    config.availability_target_pct,
    config.latency_target_ms,
  ]);

  // Auto-load when cluster or config changes — SLO calc is cheap (cache hit).
  useEffect(() => {
    if (selectedCluster) load();
  }, [selectedCluster, load]);

  const updateConfig = (patch: Partial<SloConfig>) => {
    const next = { ...config, ...patch };
    setConfig(next);
    if (selectedCluster) saveConfig(selectedCluster, next);
  };

  return (
    <PageBody>
      <PageHeader
        eyebrow="모니터"
        title="SLO Tracker"
        description="가용성 + 쿼리 지연 SLO 목표 대비 실측 + 에러 버짓 burn-down. 목표값은 클러스터별 브라우저에 저장됩니다."
        actions={
          <div className="flex items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">
              Cluster
            </label>
            <ClusterPicker selected={selectedCluster} />
          </div>
        }
      />

      {!selectedCluster ? (
        <EmptyState
          title="클러스터가 없습니다"
          description="SLO 계산을 위해 Clusters 페이지에서 먼저 등록하세요."
        />
      ) : (
        <>
          <ConfigBar
            config={config}
            onChange={updateConfig}
            loading={loading}
          />

          {err && (
            <div className="mt-4 text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
              {err}
            </div>
          )}

          {data && (
            <div className="mt-6 space-y-6">
              <div className="grid gap-4 md:grid-cols-2">
                <AvailabilityCard data={data} />
                <LatencyCard data={data} engine={engine} />
              </div>
              <Timeline buckets={data.timeline} />
            </div>
          )}

          {!data && !loading && !err && (
            <div className="mt-6 text-zinc-500 text-sm">
              데이터를 불러오는 중…
            </div>
          )}
        </>
      )}
    </PageBody>
  );
}

// ---------------------------------------------------------------------------
// Config bar — editable targets + window selector
// ---------------------------------------------------------------------------

function ConfigBar({
  config,
  onChange,
  loading,
}: {
  config: SloConfig;
  onChange: (patch: Partial<SloConfig>) => void;
  loading: boolean;
}) {
  return (
    <div className="bg-zinc-900/50 border border-zinc-800 px-4 py-3 flex flex-wrap items-center gap-4">
      <div className="flex items-center gap-2">
        <label className="text-[10px] uppercase tracking-wider text-zinc-500">
          Availability target
        </label>
        <input
          type="number"
          step="0.01"
          min="0"
          max="100"
          value={config.availability_target_pct}
          onChange={(e) =>
            onChange({ availability_target_pct: Number(e.target.value) })
          }
          className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-20 tabular-nums"
        />
        <span className="text-[10px] text-zinc-500">%</span>
      </div>
      <div className="flex items-center gap-2">
        <label className="text-[10px] uppercase tracking-wider text-zinc-500">
          Latency target
        </label>
        <input
          type="number"
          step="10"
          min="1"
          value={config.latency_target_ms}
          onChange={(e) =>
            onChange({ latency_target_ms: Number(e.target.value) })
          }
          className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-20 tabular-nums"
        />
        <span className="text-[10px] text-zinc-500">ms (avg mean)</span>
      </div>
      <div className="flex items-center gap-2 ml-auto">
        <label className="text-[10px] uppercase tracking-wider text-zinc-500">
          Window
        </label>
        <div className="flex border border-zinc-700">
          {[7, 30, 60, 90].map((d) => (
            <button
              key={d}
              onClick={() => onChange({ days: d })}
              className={`text-xs px-2 py-1 font-mono transition-colors ${
                config.days === d
                  ? "bg-amber-500 text-zinc-950"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              {d}d
            </button>
          ))}
        </div>
        {loading && (
          <span className="text-[10px] text-zinc-500 ml-2">로딩 중…</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SLO cards
// ---------------------------------------------------------------------------

function AvailabilityCard({ data }: { data: SloResponse }) {
  const a = data.availability;
  const meeting = a.actual_pct >= a.target_pct;
  const budget = a.budget_consumed_pct;
  return (
    <SloCard
      eyebrow="Availability SLO"
      title={`${fmtDecimal(a.actual_pct, 3)}%`}
      titleTone={
        meeting
          ? "emerald"
          : a.actual_pct >= a.target_pct - 0.5
            ? "amber"
            : "rose"
      }
      subtitle={`target ${fmtDecimal(a.target_pct, 2)}% · 윈도우 ${
        data.window_days
      }d`}
      budgetConsumedPct={budget}
      footer={
        <div className="grid grid-cols-3 gap-3 text-[11px]">
          <Stat
            label="Up minutes"
            value={fmtExact(a.ok_minutes)}
            sub={`of ${fmtExact(data.expected_minutes)}`}
          />
          <Stat
            label="Allowed downtime"
            value={`${fmtExact(a.allowed_downtime_minutes)}m`}
            sub="for window"
          />
          <Stat
            label="Actual downtime"
            value={`${fmtExact(a.actual_downtime_minutes)}m`}
            sub={`= ${fmtDecimal(100 - a.actual_pct, 3)}% off`}
            tone={
              a.actual_downtime_minutes > a.allowed_downtime_minutes
                ? "rose"
                : "zinc"
            }
          />
        </div>
      }
    />
  );
}

function LatencyCard({ data, engine }: { data: SloResponse; engine?: string }) {
  const l = data.latency;
  const hasData = l.compliance_pct !== null;
  const meeting = (l.compliance_pct ?? 0) >= data.availability.target_pct;
  return (
    <SloCard
      eyebrow="Query latency SLO"
      title={hasData ? `${fmtDecimal(l.compliance_pct ?? 0, 2)}%` : "—"}
      titleTone={
        !hasData
          ? "zinc"
          : meeting
            ? "emerald"
            : (l.compliance_pct ?? 0) >= data.availability.target_pct - 1
              ? "amber"
              : "rose"
      }
      subtitle={`avg(mean_time_ms) ≤ ${fmtExact(l.target_ms)}ms · 윈도우 ${
        data.window_days
      }d`}
      budgetConsumedPct={l.budget_consumed_pct}
      footer={
        <div className="grid grid-cols-3 gap-3 text-[11px]">
          <Stat
            label="Compliant min"
            value={
              hasData
                ? fmtExact(
                    Math.round(
                      ((l.compliance_pct ?? 0) / 100) * l.samples_minutes,
                    ),
                  )
                : "—"
            }
            sub={`of ${fmtExact(l.samples_minutes)}`}
          />
          <Stat
            label="Avg latency"
            value={hasData ? `${fmtDecimal(l.overall_avg_ms, 1)}ms` : "—"}
            sub="window mean"
            tone={l.overall_avg_ms > l.target_ms ? "amber" : "zinc"}
          />
          <Stat
            label="Target"
            value={`${fmtExact(l.target_ms)}ms`}
            sub="per-minute avg"
          />
        </div>
      }
      emptyNote={
        !hasData
          ? `이 윈도우에서 query_stats 샘플 없음 — ${
              isMysql(engine)
                ? "performance_schema 문장 수집(events_statements_*)이 켜져 있는지"
                : "pg_stat_statements 수집이 켜져 있는지"
            } 확인하세요.`
          : undefined
      }
    />
  );
}

type Tone = "emerald" | "amber" | "rose" | "zinc";
const TONE_TEXT: Record<Tone, string> = {
  emerald: "text-emerald-300",
  amber: "text-amber-300",
  rose: "text-rose-300",
  zinc: "text-zinc-300",
};

function SloCard({
  eyebrow,
  title,
  titleTone,
  subtitle,
  budgetConsumedPct,
  footer,
  emptyNote,
}: {
  eyebrow: string;
  title: string;
  titleTone: Tone;
  subtitle: string;
  budgetConsumedPct: number | null;
  footer: React.ReactNode;
  emptyNote?: string;
}) {
  return (
    <div className="bg-zinc-900/50 border border-zinc-800">
      <div className="px-4 py-3 border-b border-zinc-800">
        <div className="text-[10px] uppercase tracking-wider text-amber-400/70 font-mono">
          {eyebrow}
        </div>
        <div
          className={`text-4xl font-semibold tracking-tight mt-1 tabular-nums ${TONE_TEXT[titleTone]}`}
        >
          {title}
        </div>
        <div className="text-[11px] text-zinc-500 mt-1">{subtitle}</div>
      </div>
      <div className="px-4 py-3 border-b border-zinc-800">
        <BudgetBar consumedPct={budgetConsumedPct} />
      </div>
      <div className="px-4 py-3">
        {emptyNote ? (
          <div className="text-[11px] text-amber-300 border-l-2 border-amber-500/40 pl-2">
            {emptyNote}
          </div>
        ) : (
          footer
        )}
      </div>
    </div>
  );
}

function BudgetBar({ consumedPct }: { consumedPct: number | null }) {
  if (consumedPct === null) {
    return (
      <div>
        <div className="flex items-baseline justify-between text-[10px] mb-1">
          <span className="uppercase tracking-wider text-zinc-500">
            Error budget
          </span>
          <span className="text-zinc-500">N/A</span>
        </div>
        <div className="h-2 bg-zinc-800" />
      </div>
    );
  }
  const tone =
    consumedPct >= 100
      ? "bg-rose-500"
      : consumedPct >= 80
        ? "bg-rose-400"
        : consumedPct >= 50
          ? "bg-amber-400"
          : "bg-emerald-400";
  const textTone =
    consumedPct >= 80
      ? "text-rose-300"
      : consumedPct >= 50
        ? "text-amber-300"
        : "text-emerald-300";
  return (
    <div>
      <div className="flex items-baseline justify-between text-[10px] mb-1">
        <span className="uppercase tracking-wider text-zinc-500">
          Error budget consumed
        </span>
        <span className={`font-mono ${textTone}`}>
          {fmtDecimal(consumedPct, 1)}%
        </span>
      </div>
      <div className="h-2 bg-zinc-800 overflow-hidden">
        <div
          className={`h-full ${tone} transition-all`}
          style={{ width: `${Math.min(100, consumedPct)}%` }}
        />
      </div>
      <div className="text-[10px] text-zinc-600 mt-1">
        {consumedPct >= 100
          ? "버짓 소진 — 신규 배포 보류 권장"
          : consumedPct >= 50
            ? "버짓 절반 이상 소진 — 변경 일정 재검토"
            : "버짓 여유"}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  tone = "zinc",
}: {
  label: string;
  value: string;
  sub: string;
  tone?: Tone;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div
        className={`text-base font-mono mt-0.5 tabular-nums ${TONE_TEXT[tone]}`}
      >
        {value}
      </div>
      <div className="text-[10px] text-zinc-600">{sub}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Day timeline — heatmap-style strip
// ---------------------------------------------------------------------------

function Timeline({ buckets }: { buckets: SloDayBucket[] }) {
  const hasAny = useMemo(() => buckets.some((b) => !b.no_data), [buckets]);

  if (buckets.length === 0) return null;

  return (
    <div className="bg-zinc-900/50 border border-zinc-800">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-baseline justify-between">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">
            일별 타임라인
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            각 칸 = 하루. 좌측이 가장 오래된 날.
          </div>
        </div>
        <div className="flex items-center gap-3 text-[10px] text-zinc-500">
          <LegendDot tone="emerald" label="정상" />
          <LegendDot tone="amber" label="가용성 미달" />
          <LegendDot tone="rose" label="지연 미달" />
          <LegendDot tone="zinc" label="데이터 없음" />
        </div>
      </div>
      <div className="p-4 grid grid-cols-2 gap-4">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5">
            Availability
          </div>
          <Strip buckets={buckets} kind="availability" />
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5">
            Latency
          </div>
          <Strip buckets={buckets} kind="latency" />
        </div>
      </div>
      {!hasAny && (
        <div className="px-4 pb-3 text-[11px] text-amber-300/80">
          전 기간 데이터 없음 — ETL 수집기가 동작 중인지 확인하세요.
        </div>
      )}
    </div>
  );
}

function Strip({
  buckets,
  kind,
}: {
  buckets: SloDayBucket[];
  kind: "availability" | "latency";
}) {
  return (
    <div className="flex gap-[2px] flex-wrap">
      {buckets.map((b) => {
        const ok = kind === "availability" ? b.availability_ok : b.latency_ok;
        const noData =
          b.no_data ||
          (kind === "availability" && b.availability_pct === 0) ||
          (kind === "latency" && b.avg_latency_ms === 0);
        const tone = noData
          ? "bg-zinc-800 border-zinc-800"
          : ok
            ? "bg-emerald-500/60 border-emerald-500/40"
            : kind === "availability"
              ? "bg-amber-500/70 border-amber-500/50"
              : "bg-rose-500/60 border-rose-500/40";
        const titleMain =
          kind === "availability"
            ? `${b.day} · ${fmtDecimal(b.availability_pct, 2)}% up`
            : `${b.day} · avg ${fmtDecimal(b.avg_latency_ms, 1)}ms`;
        return (
          <div
            key={b.day}
            title={noData ? `${b.day} · 데이터 없음` : titleMain}
            className={`w-4 h-6 border ${tone}`}
          />
        );
      })}
    </div>
  );
}

function LegendDot({ tone, label }: { tone: Tone; label: string }) {
  const dot = {
    emerald: "bg-emerald-500/60",
    amber: "bg-amber-500/70",
    rose: "bg-rose-500/60",
    zinc: "bg-zinc-800 border border-zinc-700",
  }[tone];
  return (
    <span className="flex items-center gap-1">
      <span className={`w-3 h-3 ${dot}`} />
      {label}
    </span>
  );
}
