"use client";

import { useEffect, useState } from "react";
import {
  simulateDynamodbCapacityCost,
  type DdbCapacityCostResponse,
} from "@/lib/api-client";
import {
  Section,
  EmptyState,
  Stat,
  StatRow,
} from "@/components/design-system/page-shell";
import { fmtDecimal } from "@/lib/format";

// DynamoDB Provisioned↔On-Demand 월 비용 what-if. RCU/WCU/On-Demand/Provisioned/
// p99 등 DBA jargon은 영어 유지, 설명/empty-state는 한글. 가격 미해결 시
// "n/a"만 표기하고 가짜 달러 숫자는 절대 만들지 않는다(백엔드 honesty 계약).

const MODE_KO: Record<string, string> = {
  PROVISIONED: "Provisioned",
  PAY_PER_REQUEST: "On-Demand",
};

function usd(v: number | null | undefined): string {
  return v == null ? "n/a" : `$${fmtDecimal(v, 2)}`;
}

export function DynamoDbCapacitySimulator({
  clusterId,
}: {
  clusterId: string;
}) {
  const [data, setData] = useState<DdbCapacityCostResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setErr(null);
    setLoading(true);
    simulateDynamodbCapacityCost(clusterId)
      .then((r) => {
        if (alive) setData(r);
      })
      .catch((e) => {
        if (alive) setErr(e instanceof Error ? e.message : "fetch failed");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [clusterId]);

  return (
    <Section
      eyebrow="DynamoDB Cost"
      title="용량 모드 비용 시뮬레이션"
      description="테이블의 실제 소비 용량(consumed RCU/WCU)을 기준으로 Provisioned ↔ On-Demand 월 비용을 실시간 AWS Pricing 단가로 비교합니다. 용량(capacity) 비용만 대상이며 storage·backup·replication은 제외합니다."
    >
      <div className="bg-zinc-900/50 border border-zinc-800">
        {loading && (
          <div className="p-6 text-zinc-500 text-sm">
            소비 용량과 리전별 단가를 불러오는 중입니다…
          </div>
        )}

        {err && !loading && (
          <div className="p-4 text-xs text-rose-300 bg-rose-500/5">{err}</div>
        )}

        {!loading && !err && data && data.status === "no_data" && (
          <div className="p-4">
            <EmptyState
              eyebrow="데이터 부족"
              title="비용 비교를 위한 데이터가 부족합니다"
              description={
                data.no_data_reason ??
                "소비 용량 데이터포인트가 충분히 수집되지 않았습니다."
              }
            />
          </div>
        )}

        {!loading && !err && data && data.status === "unsupported" && (
          <div className="p-4">
            <EmptyState
              eyebrow="미지원"
              title="이 테이블은 비용 비교를 지원하지 않습니다"
              description={
                data.unsupported_reason ??
                "이 테이블 유형은 비용 시뮬레이션을 지원하지 않습니다."
              }
            />
          </div>
        )}

        {!loading &&
          !err &&
          data &&
          data.status !== "no_data" &&
          data.status !== "unsupported" && (
            <div className="p-4 space-y-4">
              {/* Header: current mode badge + region */}
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                  현재 모드
                </span>
                <span className="px-1.5 py-0.5 border text-[10px] font-mono text-violet-300 border-violet-500/40 bg-violet-500/10">
                  {data.billing_mode ? MODE_KO[data.billing_mode] : "unknown"}
                </span>
                <span className="text-[10px] text-zinc-600 font-mono ml-auto">
                  {data.region} · {fmtDecimal(data.window_hours, 0)}h 윈도우 ·{" "}
                  {fmtDecimal(data.datapoints, 0)} datapoints
                </span>
              </div>

              <StatRow cols={3}>
                <Stat
                  label="현재 월 비용"
                  value={usd(data.current_monthly_usd)}
                  hint={
                    data.billing_mode
                      ? `${MODE_KO[data.billing_mode]} 기준`
                      : undefined
                  }
                  accent="neutral"
                />
                <Stat
                  label="On-Demand 월 비용 (추정)"
                  value={usd(data.on_demand_monthly_usd)}
                  hint="consumed × $/RRU·WRU"
                  accent={
                    data.recommended_mode === "PAY_PER_REQUEST"
                      ? "emerald"
                      : "neutral"
                  }
                />
                <Stat
                  label="Provisioned 월 비용 (추정)"
                  value={usd(data.provisioned_monthly_usd)}
                  hint={
                    data.sizing
                      ? `RCU ${fmtDecimal(
                          data.sizing.rcu_per_sec,
                          0,
                        )} / WCU ${fmtDecimal(data.sizing.wcu_per_sec, 0)} /s`
                      : undefined
                  }
                  accent={
                    data.recommended_mode === "PROVISIONED"
                      ? "emerald"
                      : "neutral"
                  }
                />
              </StatRow>

              {/* Recommendation banner — only when BOTH prices resolved */}
              {data.recommended_mode &&
              data.monthly_savings_usd != null &&
              data.savings_pct != null ? (
                data.monthly_savings_usd > 0 ? (
                  <div className="border border-emerald-500/30 bg-emerald-500/5 px-4 py-3 text-sm text-emerald-200">
                    <span className="font-mono text-emerald-300">
                      {MODE_KO[data.recommended_mode]}
                    </span>
                    로 전환 시 월{" "}
                    <span className="font-mono text-emerald-300">
                      ${fmtDecimal(data.monthly_savings_usd, 2)}
                    </span>{" "}
                    ({fmtDecimal(data.savings_pct, 1)}%) 절감이 예상됩니다.
                  </div>
                ) : (
                  <div className="border border-zinc-700 bg-zinc-900/60 px-4 py-3 text-sm text-zinc-300">
                    현재 모드(
                    <span className="font-mono">
                      {data.billing_mode ? MODE_KO[data.billing_mode] : "—"}
                    </span>
                    )가 두 모드 중 더 저렴합니다 — 전환 이점이 없습니다.
                  </div>
                )
              ) : (
                <div className="border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-xs text-amber-200">
                  일부 단가를 확인하지 못해 권장 모드를 산출하지 않았습니다(아래
                  Pricing 출처 참고). 확인된 비용만 표시합니다.
                </div>
              )}

              {/* Pricing source badge (mirror the simulator PricingContext) */}
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-zinc-500 font-mono border-t border-zinc-800 pt-2.5">
                <span
                  className={`px-1.5 py-0.5 border ${
                    data.pricing_source === "aws_pricing_api"
                      ? "text-emerald-300 border-emerald-500/40 bg-emerald-500/5"
                      : "text-amber-300 border-amber-500/40 bg-amber-500/5"
                  }`}
                >
                  {data.pricing_source === "aws_pricing_api"
                    ? "Pricing API"
                    : "fallback"}
                </span>
                <span>{data.region}</span>
                {data.sizing && (
                  <span>
                    sizing basis {data.sizing.basis} · headroom{" "}
                    {fmtDecimal(data.sizing.headroom * 100, 0)}%
                  </span>
                )}
              </div>

              {/* Assumptions disclosure */}
              {data.assumptions.length > 0 && (
                <details className="text-[11px] text-zinc-500">
                  <summary className="cursor-pointer text-zinc-400 hover:text-zinc-200 select-none">
                    추정 가정 보기
                  </summary>
                  <ul className="mt-2 space-y-1">
                    {data.assumptions.map((a, i) => (
                      <li key={i} className="flex gap-1.5 leading-relaxed">
                        <span className="text-zinc-600 select-none">·</span>
                        <span>{a}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          )}
      </div>
    </Section>
  );
}
