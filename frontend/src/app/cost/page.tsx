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
        description="Application=DBOps 태그가 붙은 모든 Bedrock 호출의 비용. Cost Explorer 데이터는 약 24시간 지연됩니다."
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

      {data?.tag_warning && (
        <div className="mb-6 px-4 py-3 border border-amber-500/40 bg-amber-500/10 text-amber-200 text-sm leading-relaxed">
          <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-amber-300 mb-1">
            tag attribution warning
          </div>
          {data.tag_warning}
          <div className="mt-2">
            <a
              href="https://console.aws.amazon.com/billing/home#/preferences/tags"
              target="_blank"
              rel="noreferrer"
              className="text-xs underline hover:text-amber-100"
            >
              Open Billing → Cost allocation tags
            </a>
          </div>
        </div>
      )}

      {data?.no_data_reason && !data?.tag_warning ? (
        <EmptyState
          eyebrow="not yet tracked"
          title="Cost allocation tag is not activated"
          description={
            <>
              {data.no_data_reason}.
              <br />
              <span className="text-zinc-600">Once activated, this view backfills automatically.</span>
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
            <Stat
              label="Tagged calls"
              value="DBOps"
              hint="Application=DBOps via AIP"
              loading={false}
            />
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
