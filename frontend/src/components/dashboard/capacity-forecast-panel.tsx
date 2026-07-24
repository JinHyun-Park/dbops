"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchCapacityForecast,
  type CapacityForecastResponse,
  type CapacityMetric,
} from "@/lib/api-client";
import { fmtDecimal } from "@/lib/format";
import { engineFamily } from "@/lib/engine";

type MetricSpec = {
  key: CapacityMetric;
  label: string;
  // Convert raw stored unit → display value + suffix. Storage is in bytes;
  // connections / AAS / capacity units are scalar numbers.
  format: (n: number) => { value: string; suffix: string };
};

const _storage: MetricSpec = {
  key: "storage_bytes",
  label: "Storage",
  format: (n) => {
    const gb = n / 1024 ** 3;
    if (gb >= 1024) return { value: fmtDecimal(gb / 1024, 2), suffix: "TiB" };
    return { value: fmtDecimal(gb, 1), suffix: "GiB" };
  },
};
const _connections: MetricSpec = {
  key: "db_connections",
  label: "Connections",
  format: (n) => ({ value: fmtDecimal(n, 0), suffix: "" }),
};
const _aas: MetricSpec = {
  key: "aas",
  label: "Active Sessions (AAS)",
  format: (n) => ({ value: fmtDecimal(n, 2), suffix: "" }),
};
const _rcu: MetricSpec = {
  key: "consumed_rcu",
  label: "Read Capacity (RCU/분)",
  format: (n) => ({ value: fmtDecimal(n, 0), suffix: "RCU" }),
};
const _wcu: MetricSpec = {
  key: "consumed_wcu",
  label: "Write Capacity (WCU/분)",
  format: (n) => ({ value: fmtDecimal(n, 0), suffix: "WCU" }),
};

// Engine-specific metric tabs. Relational keeps the original list; DocDB
// forecasts connections + storage; DynamoDB forecasts provisioned throughput.
const METRICS_BY_FAMILY: Record<string, MetricSpec[]> = {
  relational: [_storage, _connections, _aas],
  documentdb: [_connections, _storage],
  dynamodb: [_wcu, _rcu],
  rds_instance: [_storage, _connections],
};

function metricsFor(engine?: string): MetricSpec[] {
  return (
    METRICS_BY_FAMILY[engineFamily(engine)] ?? METRICS_BY_FAMILY.relational
  );
}

function urgencyColor(days: number | null): {
  text: string;
  bg: string;
  border: string;
  label: string;
} {
  if (days == null)
    return {
      text: "text-emerald-300",
      bg: "bg-emerald-500/10",
      border: "border-emerald-500/40",
      label: "안정",
    };
  if (days < 30)
    return {
      text: "text-rose-300",
      bg: "bg-rose-500/10",
      border: "border-rose-500/40",
      label: "위험",
    };
  if (days < 180)
    return {
      text: "text-amber-300",
      bg: "bg-amber-500/10",
      border: "border-amber-500/40",
      label: "주의",
    };
  return {
    text: "text-emerald-300",
    bg: "bg-emerald-500/10",
    border: "border-emerald-500/40",
    label: "여유",
  };
}

export function CapacityForecastPanel({
  clusterId,
  engine,
}: {
  clusterId: string;
  engine?: string;
}) {
  const metrics = useMemo(() => metricsFor(engine), [engine]);
  const defaultMetric = metrics[0].key;

  const [active, setActive] = useState<CapacityMetric>(defaultMetric);
  const [cache, setCache] = useState<
    Partial<Record<CapacityMetric, CapacityForecastResponse>>
  >({});
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const data = cache[active];
  const metricSpec = metrics.find((m) => m.key === active) ?? metrics[0];

  const load = useCallback(
    async (m: CapacityMetric) => {
      if (cache[m]) {
        setActive(m);
        return;
      }
      setLoading(true);
      setErr(null);
      try {
        const r = await fetchCapacityForecast(clusterId, m, 30);
        setCache((prev) => ({ ...prev, [m]: r }));
        setActive(m);
      } catch (e) {
        setErr(e instanceof Error ? e.message : "fetch failed");
      } finally {
        setLoading(false);
      }
    },
    [clusterId, cache],
  );

  // Initial load — pull the engine's default metric forecast on mount /
  // whenever the selected cluster (or its engine family) changes.
  useEffect(() => {
    setCache({});
    setActive(defaultMetric);
    load(defaultMetric);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterId, defaultMetric]);

  const usagePct = useMemo(() => {
    if (!data || !data.limit) return 0;
    return Math.min(100, Math.max(0, (data.current / data.limit) * 100));
  }, [data]);

  const urgency = urgencyColor(data?.days_until_limit ?? null);
  const cur = data ? metricSpec.format(data.current) : null;
  const lim = data ? metricSpec.format(data.limit) : null;
  const p30 = data ? metricSpec.format(data.projections.d30) : null;
  const p60 = data ? metricSpec.format(data.projections.d60) : null;
  const p90 = data ? metricSpec.format(data.projections.d90) : null;

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <div className="text-sm text-zinc-200 font-medium">
            Capacity Forecast
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            최근 30일 metric_snapshots 선형 회귀로 30/60/90일 후 사용량 + 한도
            도달 시점 추정.
          </div>
        </div>
        <div className="flex items-center gap-1">
          {metrics.map((m) => (
            <button
              key={m.key}
              onClick={() => load(m.key)}
              className={`text-[10px] uppercase tracking-wider px-2 py-1 border transition-colors ${
                active === m.key
                  ? "border-amber-500/60 text-amber-300 bg-amber-500/5"
                  : "border-zinc-800 text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {loading && !data && (
        <div className="p-6 text-zinc-500 text-sm">불러오는 중…</div>
      )}
      {err && (
        <div className="p-5">
          <div className="text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
            {err}
          </div>
        </div>
      )}
      {data && data.error && (
        <div className="p-5">
          <div className="text-xs text-amber-300 border border-amber-500/40 bg-amber-500/10 px-3 py-2">
            {data.error}
          </div>
        </div>
      )}
      {/* not_applicable — e.g. on-demand DynamoDB (no provisioned ceiling to
          forecast toward). Neutral notice, not an error. */}
      {data && !data.error && data.not_applicable && (
        <div className="p-5">
          <div className="text-xs text-zinc-400 border border-zinc-700 bg-zinc-900/40 px-3 py-2">
            이 지표는 현재 클러스터에서 용량 예측을 제공하지 않습니다 —
            온디맨드(프로비저닝되지 않은) 용량이거나 예측 가능한 한도가
            없습니다.
          </div>
        </div>
      )}
      {data && !data.error && !data.not_applicable && data.samples < 7 && (
        <div className="p-5">
          <div className="text-xs text-zinc-400 border border-zinc-700 bg-zinc-900/40 px-3 py-2">
            데이터 부족 ({data.samples}개 샘플) — 신뢰성 있는 예측을 위해 최소
            7개 이상의 일별 데이터 포인트가 필요합니다.
          </div>
        </div>
      )}
      {data && !data.error && !data.not_applicable && data.samples >= 7 && (
        <div className="p-5 space-y-5">
          {/* Headline: current vs limit + urgency */}
          <div className="flex items-end justify-between gap-4 flex-wrap">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                현재
              </div>
              <div className="text-3xl text-zinc-100 tabular-nums">
                {cur!.value}
                {cur!.suffix && (
                  <span className="text-base text-zinc-500 ml-1">
                    {cur!.suffix}
                  </span>
                )}
              </div>
              <div className="text-[11px] text-zinc-500 mt-0.5">
                한도{" "}
                <span className="font-mono text-zinc-300">
                  {lim!.value}
                  {lim!.suffix && ` ${lim!.suffix}`}
                </span>{" "}
                중 {usagePct.toFixed(1)}% 사용 ·{" "}
                <span
                  className={
                    data.forecast === "growing"
                      ? "text-rose-300"
                      : data.forecast === "shrinking"
                        ? "text-sky-300"
                        : "text-zinc-400"
                  }
                >
                  {data.forecast === "growing"
                    ? "증가 추세"
                    : data.forecast === "shrinking"
                      ? "감소 추세"
                      : "안정"}
                </span>
              </div>
            </div>

            <div
              className={`px-3 py-2 border ${urgency.border} ${urgency.bg}`}
              title={
                data.days_until_limit != null
                  ? `현재 추세대로면 ${data.days_until_limit}일 후 한도에 도달`
                  : "현재 추세대로면 한도에 도달하지 않음"
              }
            >
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                한도 도달
              </div>
              <div className={`text-base font-medium ${urgency.text}`}>
                {data.days_until_limit != null
                  ? `${data.days_until_limit}일 후`
                  : "예측 한도 안전"}
              </div>
              <div className="text-[10px] text-zinc-500 mt-0.5">
                {urgency.label} · 추세 {data.slope_per_day >= 0 ? "+" : ""}
                {metricSpec.format(Math.abs(data.slope_per_day)).value}
                {metricSpec.format(Math.abs(data.slope_per_day)).suffix && (
                  <> {metricSpec.format(Math.abs(data.slope_per_day)).suffix}</>
                )}
                /일
              </div>
            </div>
          </div>

          {/* Usage bar */}
          <div>
            <div className="h-2 w-full bg-zinc-800 overflow-hidden">
              <div
                className={`h-full ${urgency.bg.replace("/10", "/40")}`}
                style={{ width: `${usagePct}%` }}
              />
            </div>
            <div className="flex justify-between text-[10px] text-zinc-600 font-mono mt-1">
              <span>0</span>
              <span>{usagePct.toFixed(1)}%</span>
              <span>
                {lim!.value}
                {lim!.suffix && ` ${lim!.suffix}`}
              </span>
            </div>
          </div>

          {/* 30/60/90 day projections */}
          <div className="grid grid-cols-3 gap-3">
            {[
              { label: "30일 후", value: p30! },
              { label: "60일 후", value: p60! },
              { label: "90일 후", value: p90! },
            ].map((p) => (
              <div
                key={p.label}
                className="border border-zinc-800 bg-zinc-950 px-3 py-2"
              >
                <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                  {p.label}
                </div>
                <div className="text-base text-zinc-100 mt-0.5 tabular-nums">
                  {p.value.value}
                  {p.value.suffix && (
                    <span className="text-[11px] text-zinc-500 ml-1">
                      {p.value.suffix}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>

          <div className="text-[10px] text-zinc-600 font-mono">
            기준: 최근 {data.days_lookback}일 · {data.samples}개 샘플 · 단순
            선형 회귀 (시즌성/스파이크 미반영)
          </div>
        </div>
      )}
    </div>
  );
}
