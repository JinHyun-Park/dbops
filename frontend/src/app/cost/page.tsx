"use client";

import { useEffect, useState } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  ReferenceDot,
} from "recharts";
import { fetchCost, apiUrl } from "@/lib/api-client";
import { fmtDecimal } from "@/lib/format";
import { useChartColors } from "@/lib/use-chart-colors";
import {
  PageBody,
  PageHeader,
  EmptyState,
  Stat,
  StatRow,
  Section,
} from "@/components/design-system/page-shell";

interface CostAnomaly {
  date: string;
  amount: number;
  baseline_mean: number;
  baseline_stddev: number;
  z_score: number;
  delta_pct: number | null;
  severity: "warning" | "critical";
}

interface CostData {
  env: string;
  range_days: number;
  total: number;
  total_tagged?: number;
  currency: string;
  daily: { date: string; amount: number }[];
  by_usage_type: { usage_type: string; amount: number; quantity: number }[];
  anomalies?: CostAnomaly[];
  no_data_reason?: string | null;
  tag_warning?: string | null;
  discovered_services?: string[];
}

interface RdsClusterCost {
  cluster: string;
  amount: number;
}

interface RdsCostData {
  env: string;
  view: "rds";
  range_days: number;
  start: string;
  end: string;
  total: number;
  currency: string;
  daily: { date: string; amount: number }[];
  by_usage_type: { usage_type: string; amount: number; quantity: number }[];
  per_cluster: RdsClusterCost[];
  per_cluster_available: boolean;
  per_cluster_tag?: string | null;
  per_cluster_note?: string | null;
  anomalies?: CostAnomaly[];
  no_data_reason?: string | null;
  discovered_services?: string[];
}

const RANGES = [7, 14, 30, 60, 90];

type CostTab = "bedrock" | "rds";

// RDS usage-type labels are like "APN1-Aurora:StorageIOUsage" or
// "APN1-InstanceUsage:db.r6g.large" — strip the region prefix and humanize the
// remaining segment so the table reads cleanly while keeping the raw value
// visible underneath (matching the Bedrock breakdown's two-line cell).
function rdsUsageLabel(ut: string): string {
  const parts = ut.split(":");
  const head = (parts[0] || ut).replace(/^[A-Z0-9]+-/, "");
  const tail = parts.slice(1).join(":");
  return tail ? `${head} · ${tail}` : head;
}

function shortLabel(ut: string): string {
  // "APN1-Bedrock:Tokens:Input:Anthropic:Claude-Sonnet-4-6" -> "Sonnet 4.6 in"
  const parts = ut.split(":");
  const dir = parts[2] || ""; // Input | Output
  const model = (parts.slice(3).join(":") || ut)
    .replace(/^Anthropic[:.-]?/, "")
    .trim();
  const direction =
    dir.toLowerCase() === "input"
      ? "in"
      : dir.toLowerCase() === "output"
        ? "out"
        : dir.toLowerCase();
  return direction ? `${model} ${direction}` : model;
}

export default function CostPage() {
  const [tab, setTab] = useState<CostTab>("bedrock");
  const [days, setDays] = useState(30);
  const [data, setData] = useState<CostData | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const colors = useChartColors();

  useEffect(() => {
    if (tab !== "bedrock") return;
    setLoading(true);
    setErr(null);
    fetchCost(days)
      .then((d) => setData(d))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, [days, tab]);

  const dailyAvg =
    data && data.daily.length > 0 ? data.total / data.daily.length : 0;
  const monthlyProjection = dailyAvg * 30;

  const isRds = tab === "rds";

  return (
    <PageBody>
      <PageHeader
        eyebrow="재무"
        title={isRds ? "Aurora / RDS 비용" : "Bedrock 비용"}
        description={
          isRds
            ? "계정의 Aurora/RDS 비용 — Cost Explorer로 사용 유형(Aurora I/O·스토리지·인스턴스 시간·백업)별로 분해합니다. 클러스터별 분리는 cost-allocation 태그를 활성화해야 합니다. CE는 약 24시간 지연됩니다."
            : "DBOps 호출의 Bedrock 비용 — Application=DBOps 태그가 박힌 Application Inference Profile을 경유합니다. Cost Explorer는 약 24시간 지연돼서 반영됩니다."
        }
        actions={
          <div className="flex items-center gap-1">
            <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
              range
            </span>
            {RANGES.map((r) => (
              <button
                key={r}
                onClick={() => setDays(r)}
                className={`text-xs px-3 py-1.5 transition-colors ${
                  days === r
                    ? "bg-amber-500 text-zinc-950"
                    : "border border-zinc-700 text-zinc-400 hover:text-zinc-100"
                }`}
              >
                {r}d
              </button>
            ))}
          </div>
        }
      />

      <div className="flex items-center gap-px mb-8 bg-zinc-800 border border-zinc-800 w-fit">
        {(
          [
            { id: "bedrock", label: "Bedrock" },
            { id: "rds", label: "Aurora / RDS" },
          ] as { id: CostTab; label: string }[]
        ).map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`text-xs px-4 py-2 transition-colors ${
              tab === t.id
                ? "bg-zinc-950 text-amber-400"
                : "bg-zinc-900/60 text-zinc-400 hover:text-zinc-100"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {isRds ? (
        <RdsCostView days={days} colors={colors} />
      ) : (
        <BedrockCostView
          data={data}
          loading={loading}
          err={err}
          days={days}
          dailyAvg={dailyAvg}
          monthlyProjection={monthlyProjection}
          colors={colors}
        />
      )}
    </PageBody>
  );
}

function BedrockCostView({
  data,
  loading,
  err,
  days,
  dailyAvg,
  monthlyProjection,
  colors,
}: {
  data: CostData | null;
  loading: boolean;
  err: string | null;
  days: number;
  dailyAvg: number;
  monthlyProjection: number;
  colors: ReturnType<typeof useChartColors>;
}) {
  return (
    <>
      {err && (
        <div className="mb-6 px-4 py-3 border border-rose-500/30 bg-rose-500/10 text-rose-300 text-sm">
          {err}
        </div>
      )}

      {data?.tag_warning && <ActivationGuide />}

      {data?.no_data_reason && !data?.tag_warning ? (
        <EmptyState
          eyebrow="추적 미시작"
          title="Cost allocation 태그가 활성화되어 있지 않습니다"
          description={
            <>
              {data.no_data_reason}.
              <br />
              <span className="text-zinc-600">
                Activate the tag, then wait ~24h. (Past spend is not back-filled
                — only post-activation calls get attributed.)
              </span>
            </>
          }
          primary={{
            href: "https://console.aws.amazon.com/billing/home#/preferences/tags",
            label: "Open Billing console",
          }}
        />
      ) : (
        <>
          <StatRow cols={3}>
            <Stat
              label={`Total ${days}d`}
              value={loading ? "···" : `$${data?.total.toFixed(2) ?? "0.00"}`}
              hint={`USD · ${data?.range_days || days} day window`}
              loading={loading}
              accent="amber"
            />
            <Stat
              label="Daily average"
              value={loading ? "···" : `$${dailyAvg.toFixed(2)}`}
              hint="avg over window"
              loading={loading}
            />
            <Stat
              label="Monthly projection"
              value={loading ? "···" : `$${monthlyProjection.toFixed(2)}`}
              hint="daily avg × 30"
              loading={loading}
            />
          </StatRow>

          {data?.anomalies && data.anomalies.length > 0 && (
            <AnomalyPanel anomalies={data.anomalies} />
          )}

          <Section eyebrow="추이" title="일별 Bedrock 사용액">
            <div className="border border-zinc-800 bg-zinc-900/50 p-4 h-72">
              {loading ? (
                <div className="text-zinc-500 text-sm">loading…</div>
              ) : !data || data.daily.length === 0 ? (
                <div className="text-zinc-500 text-sm">
                  no spend recorded yet
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart
                    data={data.daily}
                    margin={{ top: 4, right: 12, bottom: 0, left: -10 }}
                  >
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke={colors.grid}
                      vertical={false}
                    />
                    <XAxis dataKey="date" stroke={colors.axis} fontSize={10} />
                    <YAxis
                      stroke={colors.axis}
                      fontSize={10}
                      tickFormatter={(v) => `$${(Number(v) || 0).toFixed(2)}`}
                    />
                    <Tooltip
                      contentStyle={{
                        background: colors.tooltipBg,
                        border: `1px solid ${colors.tooltipBorder}`,
                        fontSize: 12,
                      }}
                      labelStyle={{ color: colors.tooltipText }}
                      formatter={(v) => [
                        `$${(Number(v) || 0).toFixed(4)}`,
                        "spend",
                      ]}
                    />
                    <Area
                      type="monotone"
                      dataKey="amount"
                      stroke={colors.amber}
                      fill={colors.amber}
                      fillOpacity={0.2}
                    />
                    {(data?.anomalies ?? []).map((a) => (
                      <ReferenceDot
                        key={a.date}
                        x={a.date}
                        y={a.amount}
                        r={5}
                        fill={
                          a.severity === "critical" ? colors.rose : "#fb923c"
                        }
                        stroke={colors.tooltipBg}
                        strokeWidth={1.5}
                      />
                    ))}
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </div>
          </Section>

          <Section eyebrow="세부 분석" title="모델 + token 방향별 비용">
            {loading ? (
              <div className="text-zinc-500 text-sm">loading…</div>
            ) : !data || data.by_usage_type.length === 0 ? (
              <div className="text-zinc-500 text-sm border border-zinc-800 p-6">
                no usage-type breakdown yet
              </div>
            ) : (
              <div className="border border-zinc-800 overflow-x-auto">
                <table className="w-full text-sm min-w-[560px]">
                  <thead className="bg-zinc-900/60 text-[10px] uppercase tracking-wider text-zinc-500">
                    <tr>
                      <th className="text-left px-4 py-2.5 font-medium">
                        model · direction
                      </th>
                      <th className="text-right px-4 py-2.5 font-medium">
                        tokens
                      </th>
                      <th className="text-right px-4 py-2.5 font-medium">
                        cost
                      </th>
                      <th className="text-right px-4 py-2.5 font-medium">
                        share
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800">
                    {data.by_usage_type.map((row, i) => {
                      const share =
                        data.total > 0 ? (row.amount / data.total) * 100 : 0;
                      return (
                        <tr
                          key={`${row.usage_type}-${i}`}
                          className="hover:bg-zinc-900/40"
                        >
                          <td className="px-4 py-2 text-zinc-200 font-mono text-xs">
                            {shortLabel(row.usage_type)}
                            <div className="text-[10px] text-zinc-600">
                              {row.usage_type}
                            </div>
                          </td>
                          <td className="px-4 py-2 text-right text-zinc-300 font-mono text-xs tabular-nums">
                            {row.quantity.toLocaleString(undefined, {
                              maximumFractionDigits: 0,
                            })}
                          </td>
                          <td className="px-4 py-2 text-right text-zinc-100 font-mono text-xs tabular-nums">
                            ${row.amount.toFixed(4)}
                          </td>
                          <td className="px-4 py-2 text-right text-amber-400 font-mono text-xs tabular-nums">
                            {share.toFixed(1)}%
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Section>
        </>
      )}

      <Section eyebrow="동작 원리">
        <div className="border border-zinc-800 bg-zinc-900/30 p-5 text-sm text-zinc-400 leading-relaxed">
          <p>
            CDK가 deploy 시점에 각 Claude 모델별로 Application Inference Profile
            (AIP) 6종을 만들고
            <code className="mx-1 px-1 py-0.5 bg-zinc-800 text-zinc-300 text-[11px] font-mono">
              Application=DBOps, Environment={data?.env || "..."}, ManagedBy=cdk
            </code>
            태그를 부여합니다.
          </p>
          <p className="mt-2">
            AgentCore Runtime이 base model 대신 AIP ARN으로 invoke하면 모든 토큰
            비용이 자동으로 태그에 attributed됩니다. AWS Billing console에서
            cost allocation tag로 "Application"을 활성화한 뒤 24시간 후부터 이
            대시보드가 실 비용을 보여줍니다.
          </p>
        </div>
      </Section>
    </>
  );
}

function RdsCostView({
  days,
  colors,
}: {
  days: number;
  colors: ReturnType<typeof useChartColors>;
}) {
  const [data, setData] = useState<RdsCostData | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErr(null);
    (async () => {
      try {
        const url = await apiUrl(`/api/cost?view=rds&days=${days}`);
        const res = await fetch(url);
        if (!res.ok) throw new Error(`RDS cost fetch failed: ${res.status}`);
        const d = (await res.json()) as RdsCostData;
        if (!cancelled) setData(d);
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [days]);

  const dailyAvg =
    data && data.daily.length > 0 ? data.total / data.daily.length : 0;
  const monthlyProjection = dailyAvg * 30;

  return (
    <>
      {err && (
        <div className="mb-6 px-4 py-3 border border-rose-500/30 bg-rose-500/10 text-rose-300 text-sm">
          {err}
        </div>
      )}

      {data?.no_data_reason && (!data || data.daily.length === 0) ? (
        <EmptyState
          eyebrow="데이터 없음"
          title="Aurora / RDS 비용 데이터가 없습니다"
          description={
            <>
              {data.no_data_reason}
              <br />
              <span className="text-zinc-600">
                Cost Explorer가 활성화돼 있는지 확인한 뒤 ~24h 후 다시
                확인하세요.
              </span>
            </>
          }
          primary={{
            href: "https://console.aws.amazon.com/cost-management/home",
            label: "Open Cost Management",
          }}
        />
      ) : (
        <>
          <StatRow cols={3}>
            <Stat
              label={`Total ${days}d`}
              value={loading ? "···" : `$${fmtDecimal(data?.total ?? 0, 2)}`}
              hint={`USD · ${data?.range_days || days} day window`}
              loading={loading}
              accent="amber"
            />
            <Stat
              label="Daily average"
              value={loading ? "···" : `$${fmtDecimal(dailyAvg, 2)}`}
              hint="avg over window"
              loading={loading}
            />
            <Stat
              label="Monthly projection"
              value={loading ? "···" : `$${fmtDecimal(monthlyProjection, 2)}`}
              hint="daily avg × 30"
              loading={loading}
            />
          </StatRow>

          {data?.anomalies && data.anomalies.length > 0 && (
            <AnomalyPanel anomalies={data.anomalies} />
          )}

          <Section eyebrow="추이" title="일별 Aurora / RDS 사용액">
            <div className="border border-zinc-800 bg-zinc-900/50 p-4 h-72">
              {loading ? (
                <div className="text-zinc-500 text-sm">loading…</div>
              ) : !data || data.daily.length === 0 ? (
                <div className="text-zinc-500 text-sm">
                  no spend recorded yet
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart
                    data={data.daily}
                    margin={{ top: 4, right: 12, bottom: 0, left: -10 }}
                  >
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke={colors.grid}
                      vertical={false}
                    />
                    <XAxis dataKey="date" stroke={colors.axis} fontSize={10} />
                    <YAxis
                      stroke={colors.axis}
                      fontSize={10}
                      tickFormatter={(v) => `$${(Number(v) || 0).toFixed(2)}`}
                    />
                    <Tooltip
                      contentStyle={{
                        background: colors.tooltipBg,
                        border: `1px solid ${colors.tooltipBorder}`,
                        fontSize: 12,
                      }}
                      labelStyle={{ color: colors.tooltipText }}
                      formatter={(v) => [
                        `$${(Number(v) || 0).toFixed(4)}`,
                        "spend",
                      ]}
                    />
                    <Area
                      type="monotone"
                      dataKey="amount"
                      stroke={colors.sky}
                      fill={colors.sky}
                      fillOpacity={0.2}
                    />
                    {(data?.anomalies ?? []).map((a) => (
                      <ReferenceDot
                        key={a.date}
                        x={a.date}
                        y={a.amount}
                        r={5}
                        fill={
                          a.severity === "critical" ? colors.rose : "#fb923c"
                        }
                        stroke={colors.tooltipBg}
                        strokeWidth={1.5}
                      />
                    ))}
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </div>
          </Section>

          <Section
            eyebrow="클러스터별"
            title="Aurora 클러스터별 비용"
            description="cost-allocation 태그가 활성화된 경우에만 클러스터 단위로 분리됩니다."
          >
            {loading ? (
              <div className="text-zinc-500 text-sm">loading…</div>
            ) : data?.per_cluster_available && data.per_cluster.length > 0 ? (
              <div className="border border-zinc-800 overflow-x-auto">
                <table className="w-full text-sm min-w-[480px]">
                  <thead className="bg-zinc-900/60 text-[10px] uppercase tracking-wider text-zinc-500">
                    <tr>
                      <th className="text-left px-4 py-2.5 font-medium">
                        cluster
                        {data.per_cluster_tag && (
                          <span className="ml-2 text-zinc-600 normal-case">
                            tag: {data.per_cluster_tag}
                          </span>
                        )}
                      </th>
                      <th className="text-right px-4 py-2.5 font-medium">
                        cost
                      </th>
                      <th className="text-right px-4 py-2.5 font-medium">
                        share
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800">
                    {data.per_cluster.map((row, i) => {
                      const share =
                        data.total > 0 ? (row.amount / data.total) * 100 : 0;
                      return (
                        <tr
                          key={`${row.cluster}-${i}`}
                          className="hover:bg-zinc-900/40"
                        >
                          <td className="px-4 py-2 text-zinc-200 font-mono text-xs">
                            {row.cluster}
                          </td>
                          <td className="px-4 py-2 text-right text-zinc-100 font-mono text-xs tabular-nums">
                            ${fmtDecimal(row.amount, 4)}
                          </td>
                          <td className="px-4 py-2 text-right text-sky-300 font-mono text-xs tabular-nums">
                            {share.toFixed(1)}%
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="border border-zinc-800 bg-zinc-900/30 p-5 text-sm text-zinc-400 leading-relaxed">
                <div className="font-mono text-[10px] tracking-[0.2em] uppercase text-zinc-500 mb-2">
                  per-cluster attribution unavailable
                </div>
                <p>
                  {data?.per_cluster_note ||
                    "클러스터별 비용 분리는 cost-allocation 태그(예: dbops:cluster)를 활성화하고 Aurora 클러스터에 부여해야 합니다."}
                </p>
                <a
                  href="https://console.aws.amazon.com/billing/home#/tags"
                  target="_blank"
                  rel="noreferrer"
                  className="inline-block mt-3 text-xs font-medium px-3 py-1.5 border border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
                >
                  Activate cost-allocation tag →
                </a>
              </div>
            )}
          </Section>

          <Section eyebrow="세부 분석" title="사용 유형별 비용">
            {loading ? (
              <div className="text-zinc-500 text-sm">loading…</div>
            ) : !data || data.by_usage_type.length === 0 ? (
              <div className="text-zinc-500 text-sm border border-zinc-800 p-6">
                no usage-type breakdown yet
              </div>
            ) : (
              <div className="border border-zinc-800 overflow-x-auto">
                <table className="w-full text-sm min-w-[560px]">
                  <thead className="bg-zinc-900/60 text-[10px] uppercase tracking-wider text-zinc-500">
                    <tr>
                      <th className="text-left px-4 py-2.5 font-medium">
                        usage type
                      </th>
                      <th className="text-right px-4 py-2.5 font-medium">
                        quantity
                      </th>
                      <th className="text-right px-4 py-2.5 font-medium">
                        cost
                      </th>
                      <th className="text-right px-4 py-2.5 font-medium">
                        share
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800">
                    {data.by_usage_type.map((row, i) => {
                      const share =
                        data.total > 0 ? (row.amount / data.total) * 100 : 0;
                      return (
                        <tr
                          key={`${row.usage_type}-${i}`}
                          className="hover:bg-zinc-900/40"
                        >
                          <td className="px-4 py-2 text-zinc-200 font-mono text-xs">
                            {rdsUsageLabel(row.usage_type)}
                            <div className="text-[10px] text-zinc-600">
                              {row.usage_type}
                            </div>
                          </td>
                          <td className="px-4 py-2 text-right text-zinc-300 font-mono text-xs tabular-nums">
                            {row.quantity.toLocaleString(undefined, {
                              maximumFractionDigits: 0,
                            })}
                          </td>
                          <td className="px-4 py-2 text-right text-zinc-100 font-mono text-xs tabular-nums">
                            ${fmtDecimal(row.amount, 4)}
                          </td>
                          <td className="px-4 py-2 text-right text-amber-400 font-mono text-xs tabular-nums">
                            {share.toFixed(1)}%
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Section>
        </>
      )}

      <Section eyebrow="동작 원리">
        <div className="border border-zinc-800 bg-zinc-900/30 p-5 text-sm text-zinc-400 leading-relaxed">
          <p>
            Aurora/RDS 비용은 AWS Cost Explorer GetCostAndUsage를 SERVICE =
            <code className="mx-1 px-1 py-0.5 bg-zinc-800 text-zinc-300 text-[11px] font-mono">
              Amazon Relational Database Service
            </code>
            로 필터링해 집계합니다. 사용 유형(USAGE_TYPE)별 분해는 추가 비용
            없이 바로 제공됩니다.
          </p>
          <p className="mt-2">
            클러스터별 분리는 Aurora 클러스터가 DBOps 소유가 아니므로 자동
            태깅되지 않습니다. AWS Billing console에서 cost-allocation 태그(예{" "}
            <code className="mx-1 px-1 py-0.5 bg-zinc-800 text-zinc-300 text-[11px] font-mono">
              dbops:cluster
            </code>
            )를 활성화하고 클러스터에 부여하면 ~24시간 후부터 클러스터별 비용이
            채워집니다. 활성화 이전 비용은 소급 적용되지 않으며, resource-level
            CE 데이터(추가 과금)는 사용하지 않습니다.
          </p>
        </div>
      </Section>
    </>
  );
}

function AnomalyPanel({ anomalies }: { anomalies: CostAnomaly[] }) {
  const counts = {
    critical: anomalies.filter((a) => a.severity === "critical").length,
    warning: anomalies.filter((a) => a.severity === "warning").length,
  };
  return (
    <Section
      eyebrow="이상치"
      title="일별 사용액 spike 감지"
      description="7일 baseline 대비 z-score > 2 + relative 50%↑ + 절대 차이 $0.5↑ 모두 만족하는 날을 표시합니다."
    >
      <div className="border border-zinc-800 bg-zinc-900/40">
        <div className="px-4 py-2 border-b border-zinc-800 flex items-center gap-3">
          {counts.critical > 0 && (
            <span className="text-[10px] px-1.5 py-0.5 border border-rose-500/40 bg-rose-500/10 text-rose-300 font-mono">
              {counts.critical} critical
            </span>
          )}
          {counts.warning > 0 && (
            <span className="text-[10px] px-1.5 py-0.5 border border-orange-500/40 bg-orange-500/10 text-orange-300 font-mono">
              {counts.warning} warning
            </span>
          )}
          <span className="text-[10px] text-zinc-500 ml-auto">
            대형 spike는 트렌드 차트의 점으로도 표시됩니다
          </span>
        </div>
        <ul className="divide-y divide-zinc-800/60">
          {anomalies.map((a) => {
            const tone =
              a.severity === "critical"
                ? "border-l-rose-500 text-rose-200"
                : "border-l-orange-500 text-orange-200";
            return (
              <li
                key={a.date}
                className={`px-4 py-2.5 border-l-2 ${tone} text-sm flex flex-wrap items-baseline gap-x-4 gap-y-1`}
              >
                <span className="font-mono text-xs text-zinc-400">
                  {a.date}
                </span>
                <span className="font-mono">${a.amount.toFixed(4)}</span>
                <span className="text-[11px] text-zinc-500 font-mono">
                  vs baseline ${a.baseline_mean.toFixed(4)}
                  {a.delta_pct !== null && (
                    <span className="ml-1.5 text-zinc-400">
                      ({a.delta_pct > 0 ? "+" : ""}
                      {a.delta_pct.toFixed(1)}%)
                    </span>
                  )}
                </span>
                <span className="text-[11px] text-zinc-500 font-mono ml-auto">
                  z={a.z_score.toFixed(2)}
                </span>
              </li>
            );
          })}
        </ul>
      </div>
    </Section>
  );
}

const GUIDE_STORAGE_KEY = "dbops_cost_guide_open";

function ActivationGuide() {
  // Default collapsed — the headline + "Activate tag →" button already
  // convey 80% of the message. localStorage carries the user's preference
  // across page loads.
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const stored = localStorage.getItem(GUIDE_STORAGE_KEY);
      if (stored === "1") setOpen(true);
    } catch {
      /* ignore */
    }
  }, []);

  const toggle = () => {
    setOpen((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(GUIDE_STORAGE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  return (
    <div className="mb-6 border border-amber-500/40 bg-amber-500/5">
      <div className="px-5 py-3 flex items-center justify-between gap-4">
        <button
          onClick={toggle}
          className="flex items-center gap-3 min-w-0 text-left flex-1 group"
          aria-expanded={open}
        >
          <span
            className={`flex-shrink-0 text-amber-300 transition-transform ${
              open ? "rotate-90" : ""
            }`}
            aria-hidden
          >
            ▸
          </span>
          <div className="min-w-0">
            <div className="font-mono text-[10px] tracking-[0.2em] uppercase text-amber-300">
              activation required for tagged attribution
            </div>
            <div className="text-xs text-amber-100/90 mt-0.5 truncate group-hover:text-amber-100">
              Application=DBOps cost allocation tag 활성화 한 번이면 끝 —{" "}
              {open ? "위 단계를 따라가세요" : "클릭해서 단계 보기"}
            </div>
          </div>
        </button>
        <a
          href="https://console.aws.amazon.com/billing/home#/tags"
          target="_blank"
          rel="noreferrer"
          className="shrink-0 text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
          onClick={(e) => e.stopPropagation()}
        >
          Activate →
        </a>
      </div>

      {open && (
        <>
          <ol className="px-5 py-4 space-y-3 text-sm text-zinc-200 border-t border-amber-500/20">
            <Step
              n={1}
              title="AWS Billing → Cost allocation tags 페이지 열기"
              body={
                <>
                  관리자 권한이 필요합니다(AWS Organizations 환경이면 management
                  account).{" "}
                  <a
                    href="https://console.aws.amazon.com/billing/home#/tags"
                    target="_blank"
                    rel="noreferrer"
                    className="text-sky-300 hover:text-sky-200 underline"
                  >
                    직접 이동
                  </a>
                </>
              }
            />
            <Step
              n={2}
              title={
                <>
                  <span className="font-mono">
                    User-defined cost allocation tags
                  </span>{" "}
                  탭에서{" "}
                  <span className="font-mono text-amber-300">Application</span>{" "}
                  찾고 체크 → <span className="text-amber-300">Activate</span>
                </>
              }
              body={
                <>
                  여유가 되면 <span className="font-mono">Environment</span>도
                  함께 체크해 env별(dev/prod) 분리도 활성화하세요.
                </>
              }
            />
            <Step
              n={3}
              title="~24시간 대기 후 이 페이지 새로고침"
              body={
                <>
                  AWS가 새 데이터를 인덱싱하면 차트와 모델별 분해표가 자동으로
                  채워집니다.{" "}
                  <span className="text-amber-300/80">
                    활성화 시점 이전 비용은 소급 적용되지 않습니다
                  </span>
                  — 과거 spend는 영구히 untagged로 남습니다.
                </>
              }
            />
          </ol>

          <div className="px-5 py-3 border-t border-amber-500/20 text-[11px] text-amber-200/70 leading-relaxed">
            Why this isn't automatic: AWS는 보안상 cost allocation tag 활성화를
            관리자 콘솔 액션으로만 허용합니다. CDK도 API도 활성화 자체는 못
            합니다. 한 번 활성화하면 이후 모든 DBOps 비용이 자동 attribute
            됩니다.
          </div>
        </>
      )}
    </div>
  );
}

function Step({
  n,
  title,
  body,
}: {
  n: number;
  title: React.ReactNode;
  body: React.ReactNode;
}) {
  return (
    <li className="flex items-start gap-3">
      <span className="flex-shrink-0 w-6 h-6 rounded-full bg-amber-500/20 border border-amber-500/40 text-amber-300 text-xs font-mono flex items-center justify-center mt-0.5">
        {n}
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-zinc-100">{title}</div>
        <div className="text-xs text-zinc-400 mt-1 leading-relaxed">{body}</div>
      </div>
    </li>
  );
}
