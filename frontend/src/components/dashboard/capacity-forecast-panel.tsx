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
  // Convert the raw stored unit into a display value + suffix. The unit belongs
  // to the LOGICAL metric, not to the per-family metric_type behind it: Aurora
  // storage_bytes and standalone-RDS free_storage_bytes are both bytes, they
  // just move in opposite directions (see `direction` on the response).
  format: (n: number) => { value: string; suffix: string };
};

const _bytes = (n: number) => {
  const gb = n / 1024 ** 3;
  if (gb >= 1024) return { value: fmtDecimal(gb / 1024, 2), suffix: "TiB" };
  return { value: fmtDecimal(gb, 1), suffix: "GiB" };
};

const _storage: MetricSpec = {
  key: "storage",
  label: "Storage",
  format: _bytes,
};
const _connections: MetricSpec = {
  key: "connections",
  label: "Connections",
  format: (n) => ({ value: fmtDecimal(n, 0), suffix: "" }),
};
const _aas: MetricSpec = {
  key: "aas",
  label: "Active Sessions (AAS)",
  format: (n) => ({ value: fmtDecimal(n, 2), suffix: "" }),
};
const _rcu: MetricSpec = {
  key: "read_capacity",
  label: "Read Capacity (RCU/분)",
  format: (n) => ({ value: fmtDecimal(n, 0), suffix: "RCU" }),
};
const _wcu: MetricSpec = {
  key: "write_capacity",
  label: "Write Capacity (WCU/분)",
  format: (n) => ({ value: fmtDecimal(n, 0), suffix: "WCU" }),
};
const _memory: MetricSpec = {
  key: "memory",
  label: "Memory",
  format: (n) => ({ value: fmtDecimal(n, 1), suffix: "%" }),
};

// Engine-specific metric tabs, mirroring _CAPACITY_METRICS_BY_FAMILY in
// api/dashboard/handler.py. Asking for a metric outside the family gets an
// explicit unsupported_metric notice, so the tabs stay in sync with the server.
const METRICS_BY_FAMILY: Record<string, MetricSpec[]> = {
  relational: [_storage, _connections, _aas],
  documentdb: [_connections, _storage],
  // Standalone RDS storage is the DEPLETING mode: FreeStorageSpace toward 0.
  rds_instance: [_storage, _connections, _aas],
  dynamodb: [_wcu, _rcu],
  // Redis/Valkey only. Memcached does not publish DatabaseMemoryUsagePercentage,
  // so the server refuses it per engine and this tab shows that refusal.
  elasticache: [_memory],
};

function metricsFor(engine?: string): MetricSpec[] {
  return (
    METRICS_BY_FAMILY[engineFamily(engine)] ?? METRICS_BY_FAMILY.relational
  );
}

// Urgency is driven by approaching_limit (the server's "act now?", already bounded
// to an actionable horizon) plus the ETA, not by the raw day count: a 219-year ETA
// is real data but not an alert.
function urgencyColor(
  days: number | null,
  approaching?: boolean,
): { text: string; bg: string; border: string; label: string } {
  const calm = {
    text: "text-emerald-300",
    bg: "bg-emerald-500/10",
    border: "border-emerald-500/40",
    label: "안정",
  };
  if (days == null) return calm;
  if (!approaching) return { ...calm, label: "여유" };
  if (days < 30)
    return {
      text: "text-rose-300",
      bg: "bg-rose-500/10",
      border: "border-rose-500/40",
      label: "위험",
    };
  return {
    text: "text-amber-300",
    bg: "bg-amber-500/10",
    border: "border-amber-500/40",
    label: "주의",
  };
}

const TREND_COPY: Record<string, { text: string; cls: string }> = {
  growing: { text: "증가 추세", cls: "text-rose-300" },
  // Free space falling is exhaustion, so it does not get a calm colour.
  depleting: { text: "소진 추세", cls: "text-rose-300" },
  shrinking: { text: "감소 추세", cls: "text-sky-300" },
  stable: { text: "안정", cls: "text-zinc-400" },
  no_data: { text: "데이터 없음", cls: "text-zinc-400" },
};

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

  // The server computes the percentage for both response modes, so nothing here
  // divides by `limit` — which is legitimately 0 when a value depletes toward a
  // floor, and 0 for an on-demand DynamoDB table with no provisioned ceiling.
  const usagePct = data?.usage_pct ?? null;
  const down = data?.direction === "down";
  const urgency = urgencyColor(
    data?.days_until_limit ?? null,
    data?.approaching_limit,
  );
  const trend = TREND_COPY[data?.forecast ?? "stable"] ?? TREND_COPY.stable;
  const cur = data ? metricSpec.format(data.current_value) : null;
  const lim = data && data.limit > 0 ? metricSpec.format(data.limit) : null;
  const proj = data?.projections;
  const refused =
    data != null &&
    (data.status === "unsupported_metric" ||
      data.status === "unknown_metric" ||
      data.status === "unknown_cluster");
  const showForecast =
    data != null && !data.error && !refused && data.samples >= 7;

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-baseline justify-between gap-3 flex-wrap">
        <div>
          <div className="text-sm text-zinc-200 font-medium">
            Capacity Forecast
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            최근 30일 metric_snapshots 선형 회귀로 30/60/90일 후 사용량 + 한도
            도달 시점 추정. 값이 한도로 늘어나는 경우와 0으로 줄어드는 경우를
            모두 다룹니다.
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
      {/* Explicit refusal: this engine does not collect the series, the name is
          not a valid metric, or the cluster is not resolvable. The server always
          sends a reason, so the panel never has to guess which one it was. */}
      {data && !data.error && refused && (
        <div className="p-5">
          <div className="text-xs text-zinc-400 border border-zinc-700 bg-zinc-900/40 px-3 py-2">
            {data.reason ??
              "이 지표는 현재 클러스터에서 용량 예측을 제공하지 않습니다."}
          </div>
        </div>
      )}
      {data && !data.error && !refused && data.samples < 7 && (
        <div className="p-5">
          <div className="text-xs text-zinc-400 border border-zinc-700 bg-zinc-900/40 px-3 py-2">
            데이터 부족 ({data.samples}개 샘플) — 신뢰성 있는 예측을 위해 최소
            7개 이상의 일별 데이터 포인트가 필요합니다.
          </div>
        </div>
      )}
      {showForecast && (
        <div className="p-5 space-y-5">
          {/* Headline: current + limit context + urgency */}
          <div className="flex items-end justify-between gap-4 flex-wrap">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                {down ? "현재 여유" : "현재"}
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
                {/* Only claim a percentage when the server computed one. A
                    depleting series has no denominator without the allocated
                    size, and an on-demand table has no ceiling at all. */}
                {usagePct != null && (
                  <>
                    {down
                      ? "할당 대비 "
                      : lim
                        ? `한도 ${lim.value}${
                            lim.suffix ? ` ${lim.suffix}` : ""
                          } 중 `
                        : ""}
                    {usagePct.toFixed(1)}% 사용 ·{" "}
                  </>
                )}
                {usagePct == null && data.grounded === false && (
                  <>한도 미확인 · </>
                )}
                <span className={trend.cls}>{trend.text}</span>
              </div>
            </div>

            <div
              className={`px-3 py-2 border ${urgency.border} ${urgency.bg}`}
              title={
                data.days_until_limit != null
                  ? `현재 추세대로면 ${data.days_until_limit}일 후 ${
                      down ? "소진" : "한도 도달"
                    }`
                  : data.reason ??
                    (down
                      ? "현재 추세대로면 소진하지 않음"
                      : "현재 추세대로면 한도에 도달하지 않음")
              }
            >
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                {down ? "소진 예상" : "한도 도달"}
              </div>
              <div className={`text-base font-medium ${urgency.text}`}>
                {data.status === "limit_reached"
                  ? down
                    ? "이미 소진"
                    : "이미 한도"
                  : data.status === "evicting"
                    ? "eviction 중"
                    : data.days_until_limit != null
                      ? `${data.days_until_limit}일 후`
                      : data.grounded === false
                        ? "예측 보류"
                        : down
                          ? "소진 예상 없음"
                          : "예측 한도 안전"}
              </div>
              <div className="text-[10px] text-zinc-500 mt-0.5">
                {urgency.label} · 추세 {data.slope_per_day >= 0 ? "+" : "-"}
                {metricSpec.format(Math.abs(data.slope_per_day)).value}
                {metricSpec.format(Math.abs(data.slope_per_day)).suffix && (
                  <> {metricSpec.format(Math.abs(data.slope_per_day)).suffix}</>
                )}
                /일
              </div>
            </div>
          </div>

          {/* Why the forecast is what it is: an eviction-recycling cache, an
              unverifiable ceiling, or no ETA at all. Never rendered as a clean
              bill of health. */}
          {data.reason && (
            <div className="text-[11px] text-zinc-400 border border-zinc-800 bg-zinc-950 px-3 py-2">
              {data.reason}
            </div>
          )}

          {/* Usage bar — only when the server produced a percentage */}
          {usagePct != null && (
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
                  {down
                    ? "할당 100%"
                    : lim
                      ? `${lim.value}${lim.suffix ? ` ${lim.suffix}` : ""}`
                      : "100%"}
                </span>
              </div>
            </div>
          )}

          {/* 30/60/90 day projections */}
          {proj && (
            <div className="grid grid-cols-3 gap-3">
              {[
                { label: "30일 후", value: metricSpec.format(proj.d30) },
                { label: "60일 후", value: metricSpec.format(proj.d60) },
                { label: "90일 후", value: metricSpec.format(proj.d90) },
              ].map((p) => (
                <div
                  key={p.label}
                  className="border border-zinc-800 bg-zinc-950 px-3 py-2"
                >
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                    {p.label}
                    {down && " 여유"}
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
          )}

          <div className="text-[10px] text-zinc-600 font-mono">
            기준: 최근 {data.days_lookback ?? 30}일 · {data.samples}개 샘플
            {data.metric_type && ` · ${data.metric_type}`}
            {data.limit_basis && ` · ${data.limit_basis}`} · 단순 선형 회귀
            (시즌성/스파이크 미반영)
          </div>
        </div>
      )}
    </div>
  );
}
