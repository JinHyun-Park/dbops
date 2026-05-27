"use client";

import { useEffect, useMemo, useState } from "react";
import {
  fetchClusters,
  fetchParameterCatalog,
  simulateDdlImpact,
  simulateParameterChange,
  simulateScaling,
  simulateUpgradeCompatibility,
  simulateUpgradeImpact,
  simulateUpgradePlan,
  type DdlImpactResponse,
  type ParameterCatalogEntry,
  type ParameterChangeResponse,
  type ScalingResponse,
  type UpgradeCompatibilityResponse,
  type UpgradeImpactResponse,
  type UpgradePlanResponse,
} from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";
import { fmtDecimal, fmtExact, fmtBytes } from "@/lib/format";

interface ClusterLite {
  cluster_id: string;
  engine?: string;
  engine_version?: string;
}

export default function SimulatorPage() {
  const [clusters, setClusters] = useState<ClusterLite[]>([]);
  const [selectedCluster, setSelectedCluster] = useState<string>("");

  useEffect(() => {
    fetchClusters()
      .then((r: unknown) => {
        // `/api/clusters` returns the array directly in this codebase.
        const list: ClusterLite[] = Array.isArray(r)
          ? (r as ClusterLite[])
          : (r as { clusters?: ClusterLite[] })?.clusters ?? [];
        setClusters(list);
        if (list.length > 0) setSelectedCluster(list[0].cluster_id);
      })
      .catch(() => {});
  }, []);

  const current = clusters.find((c) => c.cluster_id === selectedCluster);

  return (
    <PageBody>
      <PageHeader
        eyebrow="자동화"
        title="Simulator"
        description="업그레이드 · 파라미터 · 스케일링 · DDL 영향을 실제 실행 전에 추정합니다. 모든 결과는 추정치이며 프로덕션 적용 전 별도 검증 필수."
        actions={
          <div className="flex items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">
              Cluster
            </label>
            <select
              value={selectedCluster}
              onChange={(e) => setSelectedCluster(e.target.value)}
              className="bg-zinc-900 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono min-w-[280px]"
            >
              {clusters.length === 0 && <option value="">(로딩 중…)</option>}
              {clusters.map((c) => (
                <option key={c.cluster_id} value={c.cluster_id}>
                  {c.cluster_id}
                </option>
              ))}
            </select>
          </div>
        }
      />

      {!selectedCluster ? (
        <EmptyState
          title="클러스터가 없습니다"
          description="시뮬레이션을 실행하려면 Clusters 페이지에서 먼저 등록하세요."
        />
      ) : (
        <div className="space-y-8">
          <UpgradePanel clusterId={selectedCluster} engine={current?.engine} />
          <ParameterPanel clusterId={selectedCluster} />
          <ScalingPanel clusterId={selectedCluster} />
          <DdlPanel clusterId={selectedCluster} engine={current?.engine} />
        </div>
      )}
    </PageBody>
  );
}

// ---------------------------------------------------------------------------
// Upgrade Wizard — compatibility → impact methods → plan steps
// ---------------------------------------------------------------------------

function UpgradePanel({
  clusterId,
  engine,
}: {
  clusterId: string;
  engine?: string;
}) {
  const [target, setTarget] = useState("");
  const [method, setMethod] = useState<"blue_green" | "in_place" | "clone">(
    "blue_green",
  );
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [compat, setCompat] = useState<UpgradeCompatibilityResponse | null>(
    null,
  );
  const [impact, setImpact] = useState<UpgradeImpactResponse | null>(null);
  const [plan, setPlan] = useState<UpgradePlanResponse | null>(null);

  useEffect(() => {
    setCompat(null);
    setImpact(null);
    setPlan(null);
    setErr(null);
    setTarget("");
  }, [clusterId]);

  const run = async () => {
    if (!target.trim()) return;
    setLoading(true);
    setErr(null);
    try {
      const [c, i, p] = await Promise.all([
        simulateUpgradeCompatibility(clusterId, target.trim()),
        simulateUpgradeImpact(clusterId, target.trim()),
        simulateUpgradePlan(clusterId, target.trim(), method),
      ]);
      setCompat(c);
      setImpact(i);
      setPlan(p);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Section
      eyebrow="Upgrade"
      title="버전 업그레이드 시뮬레이션"
      description={`현재 ${
        engine ?? "engine"
      } 클러스터에서 target 버전으로 업그레이드할 때의 호환성 · 메서드별 시간/다운타임/리스크 · 단계별 실행 계획을 한 번에 추정합니다.`}
    >
      <div className="bg-zinc-900/50 border border-zinc-800">
        <div className="px-4 py-3 border-b border-zinc-800 flex flex-wrap items-center gap-3">
          <label className="text-[10px] uppercase tracking-wider text-zinc-500">
            Target version
          </label>
          <input
            type="text"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder="예: 16.4"
            className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-32"
          />
          <label className="text-[10px] uppercase tracking-wider text-zinc-500 ml-2">
            Method
          </label>
          <select
            value={method}
            onChange={(e) =>
              setMethod(e.target.value as "blue_green" | "in_place" | "clone")
            }
            className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1"
          >
            <option value="blue_green">blue_green</option>
            <option value="in_place">in_place</option>
            <option value="clone">clone</option>
          </select>
          <button
            onClick={run}
            disabled={loading || !target.trim()}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
          >
            {loading ? "추정 중…" : "시뮬레이션 실행"}
          </button>
        </div>

        {err && (
          <div className="p-4 text-xs text-rose-300 border-b border-zinc-800 bg-rose-500/5">
            {err}
          </div>
        )}

        {compat && (
          <div className="p-4 border-b border-zinc-800 grid gap-3 md:grid-cols-[1fr_auto]">
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                  Compatibility
                </span>
                {compat.is_compatible ? (
                  <span className="px-1.5 py-0.5 bg-emerald-500/15 text-emerald-300 border border-emerald-500/40 text-[10px]">
                    호환 가능
                  </span>
                ) : (
                  <span className="px-1.5 py-0.5 bg-rose-500/15 text-rose-300 border border-rose-500/40 text-[10px]">
                    직접 업그레이드 불가
                  </span>
                )}
              </div>
              <div className="text-xs text-zinc-300 font-mono">
                <span className="text-zinc-500">{compat.current_version}</span>
                <span className="text-zinc-600 mx-1.5">→</span>
                <span className="text-zinc-200">{compat.target_version}</span>
              </div>
              {compat.target_description && (
                <div className="text-[11px] text-zinc-500 max-w-xl">
                  {compat.target_description}
                </div>
              )}
              {!compat.is_compatible &&
                compat.valid_upgrade_targets.length > 0 && (
                  <div className="text-[11px] text-zinc-400 mt-1">
                    <span className="text-zinc-500">유효한 직접 대상: </span>
                    {compat.valid_upgrade_targets.slice(0, 8).map((v) => (
                      <button
                        key={v}
                        onClick={() => setTarget(v)}
                        className="font-mono text-zinc-200 hover:text-amber-300 mx-0.5 transition-colors"
                      >
                        {v}
                      </button>
                    ))}
                  </div>
                )}
            </div>
          </div>
        )}

        {impact && (
          <div className="px-4 py-3 border-b border-zinc-800">
            <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
              Methods · storage {fmtDecimal(impact.storage_gb, 0)} GB
            </div>
            <table className="w-full text-xs">
              <thead className="text-[10px] uppercase tracking-wider text-zinc-500 border-b border-zinc-800">
                <tr>
                  <th className="text-left py-1.5 font-medium">Method</th>
                  <th className="text-right py-1.5 font-medium">Est. time</th>
                  <th className="text-right py-1.5 font-medium">Downtime</th>
                  <th className="text-left py-1.5 font-medium pl-3">Risk</th>
                </tr>
              </thead>
              <tbody>
                {impact.methods.map((m) => (
                  <tr
                    key={m.method}
                    className={`border-b border-zinc-900 ${
                      m.method === impact.recommendation
                        ? "bg-emerald-500/5"
                        : ""
                    }`}
                  >
                    <td className="py-1.5 font-mono text-zinc-200">
                      {m.method}
                      {m.method === impact.recommendation && (
                        <span className="ml-2 text-[10px] text-emerald-400">
                          ★ 권장
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 text-right font-mono text-zinc-300 tabular-nums">
                      ~{m.estimated_minutes}분
                    </td>
                    <td className="py-1.5 text-right font-mono text-zinc-300 tabular-nums">
                      {m.downtime_text}
                    </td>
                    <td className="py-1.5 pl-3">
                      <RiskBadge risk={m.risk} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {plan && (
          <div className="px-4 py-3">
            <div className="flex items-baseline justify-between mb-2 gap-3 flex-wrap">
              <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                Plan · {plan.method}
              </div>
              <div className="text-[10px] text-zinc-500 font-mono">
                ~{plan.estimated_total_minutes}분 (계획 단계 합산 추정)
              </div>
            </div>
            <ol className="space-y-1.5">
              {plan.steps.map((s) => (
                <li
                  key={s.step}
                  className="text-xs text-zinc-300 grid grid-cols-[24px_120px_1fr] gap-3 items-baseline"
                >
                  <span className="font-mono text-zinc-600 tabular-nums">
                    {String(s.step).padStart(2, "0")}
                  </span>
                  <span className="text-zinc-200 font-medium">{s.action}</span>
                  <span className="text-zinc-400 font-mono text-[11px] break-all">
                    {s.details}
                  </span>
                </li>
              ))}
            </ol>
            <div className="mt-3 text-[11px] text-zinc-400 border-t border-zinc-800 pt-2">
              <span className="text-zinc-500 uppercase tracking-wider text-[10px] mr-2">
                Rollback
              </span>
              {plan.rollback_plan}
            </div>
          </div>
        )}

        {!compat && !loading && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            target 버전을 입력하고{" "}
            <span className="text-amber-300">시뮬레이션 실행</span>을 누르세요.
          </div>
        )}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Parameter Sandbox
// ---------------------------------------------------------------------------

function ParameterPanel({ clusterId }: { clusterId: string }) {
  const [catalog, setCatalog] = useState<ParameterCatalogEntry[]>([]);
  const [param, setParam] = useState("");
  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<ParameterChangeResponse | null>(null);

  useEffect(() => {
    fetchParameterCatalog()
      .then((r) => setCatalog(r.parameters || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    setResult(null);
    setErr(null);
    setParam("");
    setValue("");
  }, [clusterId]);

  const run = async () => {
    if (!param.trim() || !value.trim()) return;
    setLoading(true);
    setErr(null);
    try {
      const r = await simulateParameterChange(
        clusterId,
        param.trim(),
        value.trim(),
      );
      setResult(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Section
      eyebrow="Parameter"
      title="파라미터 변경 시뮬레이션"
      description="동적/정적 여부 · 재시작 필요 여부 · 영향 영역을 즉시 추정합니다."
    >
      <div className="bg-zinc-900/50 border border-zinc-800">
        <div className="px-4 py-3 border-b border-zinc-800 flex flex-wrap items-center gap-3">
          <label className="text-[10px] uppercase tracking-wider text-zinc-500">
            Parameter
          </label>
          <input
            list="param-catalog"
            value={param}
            onChange={(e) => setParam(e.target.value)}
            placeholder="예: work_mem"
            className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-48"
          />
          <datalist id="param-catalog">
            {catalog.map((p) => (
              <option key={p.name} value={p.name} />
            ))}
          </datalist>
          <label className="text-[10px] uppercase tracking-wider text-zinc-500 ml-2">
            New value
          </label>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="예: 16MB"
            className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-32"
          />
          <button
            onClick={run}
            disabled={loading || !param.trim() || !value.trim()}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
          >
            {loading ? "추정 중…" : "시뮬레이션 실행"}
          </button>
        </div>

        {err && <div className="p-4 text-xs text-rose-300">{err}</div>}

        {result && (
          <div className="p-4 grid gap-3 sm:grid-cols-3 text-xs">
            <ResultCell
              label="Parameter type"
              value={result.is_dynamic ? "dynamic" : "static"}
              tone={result.is_dynamic ? "emerald" : "amber"}
            />
            <ResultCell
              label="Restart"
              value={result.requires_restart ? "required" : "not needed"}
              tone={result.requires_restart ? "rose" : "emerald"}
            />
            <ResultCell label="Impact" value={result.impact_area} tone="zinc" />
            <div className="sm:col-span-3 text-zinc-300 border border-zinc-800 bg-zinc-900/60 px-3 py-2">
              <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                Recommendation
              </span>
              {result.recommendation}
              {!result.known && (
                <div className="text-[10px] text-amber-400 mt-1">
                  카탈로그 미등록 — 결과는 보수적 기본값입니다
                </div>
              )}
            </div>
          </div>
        )}

        {!result && !loading && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            파라미터명과 값을 입력하세요. 등록된{" "}
            <span className="text-zinc-300">{catalog.length}</span>개의
            파라미터에 대해 자동완성이 동작합니다.
          </div>
        )}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Scaling Sim
// ---------------------------------------------------------------------------

function ScalingPanel({ clusterId }: { clusterId: string }) {
  const [minAcu, setMinAcu] = useState<string>("");
  const [maxAcu, setMaxAcu] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<ScalingResponse | null>(null);

  useEffect(() => {
    setResult(null);
    setErr(null);
    setMinAcu("");
    setMaxAcu("");
  }, [clusterId]);

  const run = async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await simulateScaling(
        clusterId,
        minAcu === "" ? null : Number(minAcu),
        maxAcu === "" ? null : Number(maxAcu),
      );
      setResult(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  };

  const delta = result?.cost_impact.delta_monthly_usd ?? 0;
  const pct = result?.cost_impact.change_pct ?? 0;
  const deltaTone =
    delta > 0
      ? "text-rose-300"
      : delta < 0
        ? "text-emerald-300"
        : "text-zinc-300";

  return (
    <Section
      eyebrow="Scaling"
      title="ACU 스케일링 비용 시뮬레이션"
      description="현재 Aurora Serverless v2 ACU 범위에서 min/max를 조정했을 때의 월 추정 비용 변화를 계산합니다."
    >
      <div className="bg-zinc-900/50 border border-zinc-800">
        <div className="px-4 py-3 border-b border-zinc-800 flex flex-wrap items-center gap-3">
          <label className="text-[10px] uppercase tracking-wider text-zinc-500">
            New min ACU
          </label>
          <input
            type="number"
            step="0.5"
            min="0.5"
            value={minAcu}
            onChange={(e) => setMinAcu(e.target.value)}
            placeholder="0.5"
            className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-20 tabular-nums"
          />
          <label className="text-[10px] uppercase tracking-wider text-zinc-500 ml-2">
            New max ACU
          </label>
          <input
            type="number"
            step="0.5"
            min="0.5"
            value={maxAcu}
            onChange={(e) => setMaxAcu(e.target.value)}
            placeholder="4"
            className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-20 tabular-nums"
          />
          <button
            onClick={run}
            disabled={loading}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
          >
            {loading ? "추정 중…" : "비용 추정"}
          </button>
        </div>

        {err && <div className="p-4 text-xs text-rose-300">{err}</div>}

        {result && (
          <div className="p-4 space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <RangeCard
                label="현재"
                min={result.current.min_acu}
                max={result.current.max_acu}
                monthly={result.cost_impact.current_monthly_usd}
                tone="zinc"
              />
              <RangeCard
                label="제안"
                min={result.proposed.min_acu}
                max={result.proposed.max_acu}
                monthly={result.cost_impact.proposed_monthly_usd}
                tone={delta > 0 ? "amber" : "emerald"}
                deltaPct={pct}
              />
            </div>
            <div className="grid gap-2 sm:grid-cols-[1fr_auto] items-baseline border-t border-zinc-800 pt-2.5">
              <div className="text-xs text-zinc-400">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                  월 차액
                </span>
                <span className={`font-mono ${deltaTone}`}>
                  {delta > 0 ? "+" : ""}${fmtDecimal(delta, 2)} / month
                </span>
              </div>
              <div className="text-[10px] text-zinc-500 font-mono">
                {result.cost_assumption}
              </div>
            </div>
            {result.warnings.length > 0 && (
              <ul className="space-y-1 mt-2">
                {result.warnings.map((w, i) => (
                  <li
                    key={i}
                    className="text-[11px] text-amber-300 border-l-2 border-amber-500/40 pl-2"
                  >
                    {w}
                  </li>
                ))}
              </ul>
            )}
            <div className="text-[11px] text-zinc-500">{result.notes}</div>
          </div>
        )}

        {!result && !loading && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            min/max ACU를 입력하면 라이브 클러스터의 현재 범위와 비교합니다. 빈
            값이면 현재값을 그대로 사용합니다.
          </div>
        )}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// DDL Impact
// ---------------------------------------------------------------------------

function DdlPanel({
  clusterId,
  engine,
}: {
  clusterId: string;
  engine?: string;
}) {
  const [ddl, setDdl] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<DdlImpactResponse | null>(null);

  const isPg = (engine || "").includes("postgres");
  const sample = useMemo(
    () =>
      isPg
        ? "CREATE INDEX CONCURRENTLY idx_orders_customer ON orders (customer_id);"
        : "ALTER TABLE orders ADD COLUMN region VARCHAR(8) DEFAULT 'kr';",
    [isPg],
  );

  useEffect(() => {
    setResult(null);
    setErr(null);
    setDdl("");
  }, [clusterId]);

  const run = async () => {
    if (!ddl.trim()) return;
    setLoading(true);
    setErr(null);
    try {
      const r = await simulateDdlImpact(clusterId, ddl);
      setResult(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Section
      eyebrow="DDL"
      title="DDL 영향 시뮬레이션"
      description="ALTER / CREATE INDEX 등 DDL을 실행했을 때의 락 타입 · 예상 소요 시간 · 디스크 추가 사용량을 추정합니다."
    >
      <div className="bg-zinc-900/50 border border-zinc-800">
        <div className="px-4 py-3 border-b border-zinc-800 flex flex-wrap items-center gap-3">
          <label className="text-[10px] uppercase tracking-wider text-zinc-500">
            DDL SQL
          </label>
          <button
            onClick={() => setDdl(sample)}
            type="button"
            className="text-[10px] text-zinc-500 hover:text-zinc-300 underline underline-offset-2 font-mono"
          >
            샘플 채우기
          </button>
          <button
            onClick={run}
            disabled={loading || !ddl.trim()}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
          >
            {loading ? "추정 중…" : "영향 추정"}
          </button>
        </div>
        <textarea
          value={ddl}
          onChange={(e) => setDdl(e.target.value)}
          rows={3}
          spellCheck={false}
          placeholder={sample}
          className="w-full bg-zinc-950 border-0 border-b border-zinc-800 text-zinc-200 text-xs px-4 py-3 font-mono resize-y focus:outline-none focus:ring-1 focus:ring-amber-500"
        />

        {err && <div className="p-4 text-xs text-rose-300">{err}</div>}

        {result && (
          <div className="p-4 space-y-3">
            <div className="grid gap-3 sm:grid-cols-4 text-xs">
              <ResultCell label="Table" value={result.table} tone="zinc" mono />
              <ResultCell
                label="Rows"
                value={fmtExact(result.table_info.rows)}
                tone="zinc"
                mono
              />
              <ResultCell
                label="Est. duration"
                value={`~${fmtExact(result.estimated_seconds)} s`}
                tone={
                  result.estimated_seconds > 600
                    ? "rose"
                    : result.estimated_seconds > 60
                      ? "amber"
                      : "emerald"
                }
                mono
              />
              <ResultCell
                label="Lock"
                value={result.lock_type}
                tone={result.online_ddl_possible ? "emerald" : "amber"}
                mono
              />
            </div>
            <div className="grid gap-3 sm:grid-cols-2 text-[11px]">
              <div className="text-zinc-400">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                  Table size
                </span>
                <span className="font-mono text-zinc-300">
                  {fmtBytes(result.table_info.size_mb * 1024 * 1024)}
                </span>
              </div>
              {result.disk_space_needed_mb > 0 && (
                <div className="text-amber-300">
                  <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                    Disk needed
                  </span>
                  <span className="font-mono">
                    +{fmtBytes(result.disk_space_needed_mb * 1024 * 1024)}
                  </span>
                </div>
              )}
            </div>
            <div className="text-xs text-zinc-300 border border-zinc-800 bg-zinc-900/60 px-3 py-2">
              <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                Recommendation
              </span>
              {result.recommendation}
            </div>
          </div>
        )}

        {!result && !loading && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            DDL SQL을 입력하면 테이블 크기 추정과 락/온라인 가능 여부를
            분석합니다.
          </div>
        )}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Shared small components
// ---------------------------------------------------------------------------

function RiskBadge({ risk }: { risk: string }) {
  const tone =
    risk === "low"
      ? "text-emerald-300 border-emerald-500/40 bg-emerald-500/10"
      : risk === "medium"
        ? "text-amber-300 border-amber-500/40 bg-amber-500/10"
        : risk === "moderate"
          ? "text-amber-400 border-amber-500/50 bg-amber-500/15"
          : "text-zinc-400 border-zinc-700 bg-zinc-900/40";
  return (
    <span className={`px-1.5 py-0.5 border text-[10px] font-mono ${tone}`}>
      {risk}
    </span>
  );
}

type Tone = "emerald" | "amber" | "rose" | "zinc";
const TONE_CLASSES: Record<Tone, string> = {
  emerald: "text-emerald-300 border-emerald-500/40 bg-emerald-500/5",
  amber: "text-amber-300 border-amber-500/40 bg-amber-500/5",
  rose: "text-rose-300 border-rose-500/40 bg-rose-500/5",
  zinc: "text-zinc-300 border-zinc-800 bg-zinc-900/60",
};

function ResultCell({
  label,
  value,
  tone,
  mono = false,
}: {
  label: string;
  value: string;
  tone: Tone;
  mono?: boolean;
}) {
  return (
    <div className={`border px-3 py-2 ${TONE_CLASSES[tone]}`}>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-0.5">
        {label}
      </div>
      <div className={mono ? "text-xs font-mono break-all" : "text-xs"}>
        {value}
      </div>
    </div>
  );
}

function RangeCard({
  label,
  min,
  max,
  monthly,
  tone,
  deltaPct,
}: {
  label: string;
  min: number;
  max: number;
  monthly: number;
  tone: Tone;
  deltaPct?: number;
}) {
  return (
    <div className={`border px-3 py-2 ${TONE_CLASSES[tone]}`}>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
        {label}
      </div>
      <div className="text-base font-mono tabular-nums">
        {fmtDecimal(min, 1)}
        <span className="text-zinc-600 mx-1">→</span>
        {fmtDecimal(max, 1)}
        <span className="text-[10px] text-zinc-500 ml-1">ACU</span>
      </div>
      <div className="text-[11px] text-zinc-400 mt-1 font-mono">
        ${fmtDecimal(monthly, 2)}/mo
        {deltaPct !== undefined && Math.abs(deltaPct) > 0.1 && (
          <span
            className={`ml-2 ${
              deltaPct > 0 ? "text-rose-300" : "text-emerald-300"
            }`}
          >
            ({deltaPct > 0 ? "+" : ""}
            {fmtDecimal(deltaPct, 1)}%)
          </span>
        )}
      </div>
    </div>
  );
}
