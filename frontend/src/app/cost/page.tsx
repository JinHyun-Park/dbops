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
} from "recharts";
import { fetchCost } from "@/lib/api-client";
import { PageBody, PageHeader, EmptyState, Stat, StatRow, Section } from "@/components/design-system/page-shell";

interface CostData {
  env: string;
  range_days: number;
  total: number;
  total_tagged?: number;
  total_all_bedrock?: number;
  currency: string;
  daily: { date: string; amount: number }[];
  by_usage_type: { usage_type: string; amount: number; quantity: number }[];
  no_data_reason?: string | null;
  tag_warning?: string | null;
  discovered_services?: string[];
}

const RANGES = [7, 14, 30, 60, 90];

function shortLabel(ut: string): string {
  // "APN1-Bedrock:Tokens:Input:Anthropic:Claude-Sonnet-4-6" -> "Sonnet 4.6 in"
  const parts = ut.split(":");
  const dir = parts[2] || ""; // Input | Output
  const model = (parts.slice(3).join(":") || ut).replace(/^Anthropic[:.-]?/, "").trim();
  const direction = dir.toLowerCase() === "input" ? "in" : dir.toLowerCase() === "output" ? "out" : dir.toLowerCase();
  return direction ? `${model} ${direction}` : model;
}

export default function CostPage() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<CostData | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setErr(null);
    fetchCost(days)
      .then((d) => setData(d))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, [days]);

  const dailyAvg = data && data.daily.length > 0 ? data.total / data.daily.length : 0;
  const monthlyProjection = dailyAvg * 30;

  return (
    <PageBody>
      <PageHeader
        eyebrow="finance"
        title="Bedrock cost"
        description="DBOps에서 호출하는 모든 Bedrock 비용 — `Application=DBOps` 태그가 박힌 Application Inference Profile을 경유. 정확한 attribution을 위해 AWS Billing → Cost allocation tags에서 `Application` 태그를 활성화해야 합니다(활성화 후 ~24h 뒤부터 차트 반영, 과거 비용은 소급 안 됨)."
        actions={
          <div className="flex items-center gap-1">
            <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">range</span>
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

      {err && (
        <div className="mb-6 px-4 py-3 border border-rose-500/30 bg-rose-500/10 text-rose-300 text-sm">
          {err}
        </div>
      )}

      {data?.tag_warning && <ActivationGuide />}

      {data?.no_data_reason && !data?.tag_warning ? (
        <EmptyState
          eyebrow="not yet tracked"
          title="Cost allocation tag is not activated"
          description={
            <>
              {data.no_data_reason}.
              <br />
              <span className="text-zinc-600">
                Activate the tag, then wait ~24h. (Past spend is not back-filled — only post-activation calls get attributed.)
              </span>
            </>
          }
          primary={{ href: "https://console.aws.amazon.com/billing/home#/preferences/tags", label: "Open Billing console" }}
        />
      ) : (
        <>
          <StatRow cols={4}>
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
            {(() => {
              const acct = data?.total_all_bedrock ?? 0;
              const headline = data?.total ?? 0;
              const attentionAccent: "amber" | "neutral" = acct > 0 && headline === 0 ? "amber" : "neutral";
              return (
                <Stat
                  label="Account-wide Bedrock"
                  value={loading ? "···" : `$${acct.toFixed(2)}`}
                  hint="all Bedrock in account (not DBOps-only)"
                  loading={loading}
                  accent={attentionAccent}
                />
              );
            })()}
          </StatRow>

          <Section eyebrow="trend" title="Daily Bedrock spend">
            <div className="border border-zinc-800 bg-zinc-900/50 p-4 h-72">
              {loading ? (
                <div className="text-zinc-500 text-sm">loading…</div>
              ) : !data || data.daily.length === 0 ? (
                <div className="text-zinc-500 text-sm">no spend recorded yet</div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={data.daily} margin={{ top: 4, right: 12, bottom: 0, left: -10 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#3f3f46" vertical={false} />
                    <XAxis dataKey="date" stroke="#71717a" fontSize={10} />
                    <YAxis
                      stroke="#71717a"
                      fontSize={10}
                      tickFormatter={(v) => `$${(Number(v) || 0).toFixed(2)}`}
                    />
                    <Tooltip
                      contentStyle={{
                        background: "#18181b",
                        border: "1px solid #3f3f46",
                        fontSize: 12,
                      }}
                      labelStyle={{ color: "#a1a1aa" }}
                      formatter={(v) => [`$${(Number(v) || 0).toFixed(4)}`, "spend"]}
                    />
                    <Area
                      type="monotone"
                      dataKey="amount"
                      stroke="#fbbf24"
                      fill="#fbbf24"
                      fillOpacity={0.2}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </div>
          </Section>

          <Section eyebrow="breakdown" title="Cost by model + token direction">
            {loading ? (
              <div className="text-zinc-500 text-sm">loading…</div>
            ) : !data || data.by_usage_type.length === 0 ? (
              <div className="text-zinc-500 text-sm border border-zinc-800 p-6">
                no usage-type breakdown yet
              </div>
            ) : (
              <div className="border border-zinc-800 overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-900/60 text-[10px] uppercase tracking-wider text-zinc-500">
                    <tr>
                      <th className="text-left px-4 py-2.5 font-medium">model · direction</th>
                      <th className="text-right px-4 py-2.5 font-medium">tokens</th>
                      <th className="text-right px-4 py-2.5 font-medium">cost</th>
                      <th className="text-right px-4 py-2.5 font-medium">share</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800">
                    {data.by_usage_type.map((row, i) => {
                      const share = data.total > 0 ? (row.amount / data.total) * 100 : 0;
                      return (
                        <tr key={`${row.usage_type}-${i}`} className="hover:bg-zinc-900/40">
                          <td className="px-4 py-2 text-zinc-200 font-mono text-xs">
                            {shortLabel(row.usage_type)}
                            <div className="text-[10px] text-zinc-600">{row.usage_type}</div>
                          </td>
                          <td className="px-4 py-2 text-right text-zinc-300 font-mono text-xs tabular-nums">
                            {row.quantity.toLocaleString(undefined, { maximumFractionDigits: 0 })}
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

      <Section eyebrow="how it works">
        <div className="border border-zinc-800 bg-zinc-900/30 p-5 text-sm text-zinc-400 leading-relaxed">
          <p>
            CDK가 deploy 시점에 각 Claude 모델별로 Application Inference Profile (AIP) 6종을 만들고
            <code className="mx-1 px-1 py-0.5 bg-zinc-800 text-zinc-300 text-[11px] font-mono">
              Application=DBOps, Environment={data?.env || "..."}, ManagedBy=cdk
            </code>
            태그를 부여합니다.
          </p>
          <p className="mt-2">
            AgentCore Runtime이 base model 대신 AIP ARN으로 invoke하면 모든 토큰 비용이 자동으로
            태그에 attributed됩니다. AWS Billing console에서 cost allocation tag로
            "Application"을 활성화한 뒤 24시간 후부터 이 대시보드가 실 비용을 보여줍니다.
          </p>
        </div>
      </Section>
    </PageBody>
  );
}

function ActivationGuide() {
  return (
    <div className="mb-6 border border-amber-500/40 bg-amber-500/5">
      <div className="px-5 py-4 border-b border-amber-500/30 flex items-center justify-between gap-4">
        <div>
          <div className="font-mono text-[10px] tracking-[0.2em] uppercase text-amber-300 mb-1">
            cost attribution — one-time activation needed
          </div>
          <div className="text-sm text-amber-100 leading-snug">
            DBOps Bedrock 호출은 이미 <span className="font-mono text-amber-300">Application=DBOps</span> 태그가 박힌
            Application Inference Profile을 경유합니다. AWS Billing 콘솔에서 그 태그를{" "}
            <span className="text-amber-300">Cost allocation tag</span>으로 활성화하면 Cost Explorer가 DBOps 비용만 분리해서 attribute합니다.
          </div>
        </div>
        <a
          href="https://console.aws.amazon.com/billing/home#/tags"
          target="_blank"
          rel="noreferrer"
          className="shrink-0 text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
        >
          Activate tag →
        </a>
      </div>

      <ol className="px-5 py-4 space-y-3 text-sm text-zinc-200">
        <Step
          n={1}
          title="AWS Billing → Cost allocation tags 페이지 열기"
          body={
            <>
              관리자 권한이 필요합니다(AWS Organizations 환경이면 management account).{" "}
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
              <span className="font-mono">User-defined cost allocation tags</span> 탭에서{" "}
              <span className="font-mono text-amber-300">Application</span> 찾고 체크 → <span className="text-amber-300">Activate</span>
            </>
          }
          body={
            <>
              여유가 되면 <span className="font-mono">Environment</span>도 함께 체크해 env별(dev/prod) 분리도 활성화하세요.
            </>
          }
        />
        <Step
          n={3}
          title="~24시간 대기 후 이 페이지 새로고침"
          body={
            <>
              AWS가 새 데이터를 인덱싱하면 차트와 모델별 분해표가 자동으로 채워집니다.{" "}
              <span className="text-amber-300/80">활성화 시점 이전 비용은 소급 적용되지 않습니다</span>—
              과거 spend는 영구히 untagged로 남습니다.
            </>
          }
        />
      </ol>

      <div className="px-5 py-3 border-t border-amber-500/20 text-[11px] text-amber-200/70 leading-relaxed">
        Why this isn't automatic: AWS는 보안상 cost allocation tag 활성화를 관리자 콘솔 액션으로만 허용합니다. CDK도 API도 활성화 자체는 못 합니다. 한 번 활성화하면 이후 모든 DBOps 비용이 자동 attribute 됩니다.
      </div>
    </div>
  );
}

function Step({ n, title, body }: { n: number; title: React.ReactNode; body: React.ReactNode }) {
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
