"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  fetchMultiClusterOverview,
  fetchAlertRules,
  fetchCost,
} from "@/lib/api-client";
import { OnboardingModal, useOnboarding } from "@/components/onboarding-modal";

interface ClusterRow {
  cluster_id: string;
  engine: string;
  status: string;
  cpu: number | string | null;
  aas: number | string | null;
  conn_active: number | string | null;
  conn_idle: number | string | null;
  blocking_count: number | string | null;
  deadlocks: number | string | null;
}

interface AlertRule {
  enabled: boolean;
  last_triggered_at: string | null;
}

function n(v: unknown): number {
  if (v === null || v === undefined) return 0;
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

export default function HomePage() {
  const [clusters, setClusters] = useState<ClusterRow[]>([]);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [cost7d, setCost7d] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const onboarding = useOnboarding();

  useEffect(() => {
    Promise.allSettled([
      fetchMultiClusterOverview(),
      fetchAlertRules(),
      fetchCost(7),
    ])
      .then(([cs, rs, costRes]) => {
        if (cs.status === "fulfilled") setClusters(cs.value.clusters || []);
        if (rs.status === "fulfilled") setRules(rs.value.rules || []);
        if (costRes.status === "fulfilled") setCost7d(costRes.value.total ?? 0);
      })
      .finally(() => setLoading(false));
  }, []);

  const total = clusters.length;
  const healthy = clusters.filter((c) => c.status === "available").length;
  const blockingCount = clusters.reduce((s, c) => s + n(c.blocking_count), 0);
  const recentTriggered = rules.filter((r) => {
    if (!r.last_triggered_at) return false;
    return (
      Date.now() - new Date(r.last_triggered_at).getTime() < 24 * 3600 * 1000
    );
  }).length;
  const enabledRules = rules.filter((r) => r.enabled).length;

  return (
    <div className="max-w-7xl mx-auto p-8">
      <header className="mb-10 flex items-start justify-between gap-6">
        <div>
          <div className="font-mono text-[11px] tracking-[0.25em] text-amber-400/70 uppercase mb-2">
            운영 콘솔
          </div>
          <h1 className="text-4xl font-semibold tracking-tight text-zinc-50">
            한눈에 보는 Aurora.
          </h1>
          <p className="mt-3 text-zinc-400 max-w-2xl">
            AI agent + 실시간 메트릭 + DBA 등급 제어를 등록된 모든 클러스터에서.
            <kbd className="mx-1.5 px-1.5 py-0.5 bg-zinc-800 border border-zinc-700 rounded text-[10px] font-mono">
              ⌘K
            </kbd>
            로 command palette 열기.
          </p>
        </div>
        <button
          onClick={onboarding.reopen}
          className="shrink-0 inline-flex items-center gap-1.5 text-xs px-3 py-1.5 border border-zinc-700 text-zinc-300 hover:border-amber-500/50 hover:text-amber-300 transition-colors"
          title="튜토리얼 다시 보기"
        >
          <span className="text-sm leading-none">?</span>
          <span>튜토리얼</span>
        </button>
      </header>

      <section className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-px bg-zinc-800 border border-zinc-800 mb-10">
        <Stat
          label="클러스터"
          value={total}
          hint={`${healthy}/${total} available`}
          loading={loading}
        />
        <Stat
          label="활성 알림 규칙"
          value={enabledRules}
          hint={
            recentTriggered > 0
              ? `최근 24h ${recentTriggered}건 발화`
              : "최근 발화 없음"
          }
          loading={loading}
          accent={recentTriggered > 0 ? "amber" : "zinc"}
        />
        <Stat
          label="Blocking locks"
          value={blockingCount}
          hint={blockingCount > 0 ? "즉시 확인 필요" : "이상 없음"}
          loading={loading}
          accent={blockingCount > 0 ? "rose" : "emerald"}
        />
        <Stat
          label="Bedrock 7d"
          value={cost7d === null ? "—" : `$${cost7d.toFixed(2)}`}
          hint={cost7d === null ? "태그 미활성화" : "태그 기준 사용액"}
          loading={loading}
          accent={cost7d && cost7d > 50 ? "amber" : "zinc"}
        />
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 border border-zinc-800 bg-zinc-900/50">
          <div className="flex items-baseline justify-between px-5 py-3 border-b border-zinc-800">
            <div>
              <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500">
                fleet
              </div>
              <div className="text-sm text-zinc-200 mt-0.5">
                등록된 클러스터
              </div>
            </div>
            <Link
              href="/fleet"
              className="text-xs text-amber-400/90 hover:text-amber-300 transition-colors"
            >
              전체 보기 →
            </Link>
          </div>
          {loading ? (
            <div className="p-8 text-sm text-zinc-500">불러오는 중…</div>
          ) : clusters.length === 0 ? (
            <EmptyFleet />
          ) : (
            <table className="w-full text-sm">
              <thead className="text-[10px] uppercase tracking-wider text-zinc-500 border-b border-zinc-800">
                <tr>
                  <th className="text-left px-5 py-2 font-medium">cluster</th>
                  <th className="text-right px-5 py-2 font-medium">cpu</th>
                  <th className="text-right px-5 py-2 font-medium">aas</th>
                  <th className="text-right px-5 py-2 font-medium">conn</th>
                  <th className="text-right px-5 py-2 font-medium">blocks</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800">
                {clusters.slice(0, 8).map((c) => {
                  const cpu = n(c.cpu);
                  const aas = n(c.aas);
                  const conn = n(c.conn_active) + n(c.conn_idle);
                  const blk = n(c.blocking_count);
                  return (
                    <tr
                      key={c.cluster_id}
                      className="hover:bg-zinc-900/80 transition-colors"
                    >
                      <td className="px-5 py-2 text-zinc-200 font-mono text-xs">
                        <Link
                          href={`/dashboard?cluster=${encodeURIComponent(
                            c.cluster_id,
                          )}`}
                          className="hover:text-amber-300"
                        >
                          {c.cluster_id}
                        </Link>
                        <div className="text-[10px] text-zinc-500 mt-0.5">
                          {c.engine}
                        </div>
                      </td>
                      <td
                        className={`px-5 py-2 text-right font-mono text-xs ${
                          cpu > 80
                            ? "text-rose-400"
                            : cpu > 60
                              ? "text-amber-400"
                              : "text-zinc-300"
                        }`}
                      >
                        {c.cpu === null ? "—" : `${cpu.toFixed(1)}%`}
                      </td>
                      <td
                        className={`px-5 py-2 text-right font-mono text-xs ${
                          aas > 2 ? "text-amber-400" : "text-zinc-300"
                        }`}
                      >
                        {c.aas === null ? "—" : aas.toFixed(2)}
                      </td>
                      <td className="px-5 py-2 text-right font-mono text-xs text-zinc-300">
                        {conn || "—"}
                      </td>
                      <td
                        className={`px-5 py-2 text-right font-mono text-xs ${
                          blk > 0 ? "text-rose-400" : "text-zinc-500"
                        }`}
                      >
                        {blk || "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        <div className="border border-zinc-800 bg-zinc-900/50 p-5">
          <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 mb-1">
            빠른 작업
          </div>
          <div className="text-sm text-zinc-200 mb-5">자주 쓰는 진입점</div>
          <div className="space-y-2.5">
            <QuickLink
              href="/chat"
              title="Agent에게 물어보기"
              hint="자연어 분석"
            />
            <QuickLink
              href="/query-lab"
              title="SQL 분석"
              hint="EXPLAIN + index 추천"
            />
            <QuickLink href="/alerts" title="알림 관리" hint="규칙 + 구독자" />
            <QuickLink
              href="/clusters"
              title="클러스터 등록"
              hint="cross-account 지원"
            />
            <QuickLink
              href="/approvals"
              title="승인 대기"
              hint="쓰기 작업 검토"
            />
          </div>
        </div>
      </section>
      <OnboardingModal open={onboarding.open} onClose={onboarding.close} />
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  loading,
  accent = "zinc",
}: {
  label: string;
  value: number | string;
  hint?: string;
  loading: boolean;
  accent?: "zinc" | "amber" | "rose" | "emerald";
}) {
  const tone = {
    zinc: "text-zinc-100",
    amber: "text-amber-400",
    rose: "text-rose-400",
    emerald: "text-emerald-400",
  }[accent];
  return (
    <div className="bg-zinc-950 px-6 py-5">
      <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-zinc-500 mb-2">
        {label}
      </div>
      <div
        className={`text-3xl font-semibold tracking-tight tabular-nums ${tone}`}
      >
        {loading ? <span className="text-zinc-600">···</span> : value}
      </div>
      {hint && <div className="text-[11px] text-zinc-500 mt-1">{hint}</div>}
    </div>
  );
}

function QuickLink({
  href,
  title,
  hint,
}: {
  href: string;
  title: string;
  hint: string;
}) {
  return (
    <Link
      href={href}
      className="group flex items-start gap-3 -mx-2 px-2 py-2 rounded hover:bg-zinc-800/60 transition-colors"
    >
      <span className="w-1 h-3.5 mt-1 bg-zinc-800 group-hover:bg-amber-400 transition-colors" />
      <div className="flex-1">
        <div className="text-sm text-zinc-200 group-hover:text-zinc-50">
          {title}
        </div>
        <div className="text-[11px] text-zinc-500">{hint}</div>
      </div>
      <span className="text-zinc-700 group-hover:text-amber-400 transition-colors">
        →
      </span>
    </Link>
  );
}

function EmptyFleet() {
  return (
    <div className="p-10 text-center">
      <div className="font-mono text-[10px] tracking-[0.2em] uppercase text-zinc-600 mb-2">
        등록된 클러스터 없음
      </div>
      <div className="text-sm text-zinc-400 mb-5">
        첫 Aurora 클러스터를 등록하면 메트릭 수집이 시작됩니다.
      </div>
      <Link
        href="/clusters"
        className="inline-block text-xs font-medium px-3 py-1.5 border border-amber-500/40 text-amber-300 hover:bg-amber-500/10 transition-colors"
      >
        + 클러스터 등록
      </Link>
    </div>
  );
}
