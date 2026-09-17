"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import {
  fetchParameterCatalog,
  simulateDdlImpact,
  simulateElasticacheNodeResize,
  simulateParameterChange,
  simulateRdsInstanceRightsizing,
  simulateScaling,
  simulateUpgradeCompatibility,
  simulateUpgradeImpact,
  simulateUpgradePlan,
  type DdlImpactResponse,
  type ElasticacheNodeResizeResponse,
  type ParameterCatalogEntry,
  type ParameterChangeResponse,
  type RdsRightsizingResponse,
  type ScalingResponse,
  type ScalingUnitPricing,
  type UpgradeCompatibilityResponse,
  type UpgradeImpactResponse,
  type UpgradePlanResponse,
} from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  Section,
  EmptyState,
  Stat,
  StatRow,
} from "@/components/design-system/page-shell";
import { fmtDecimal, fmtExact, fmtBytes } from "@/lib/format";
import { useSelectedCluster } from "@/lib/use-selected-cluster";
import { ClusterPicker } from "@/components/design-system/cluster-picker";
import { engineFamily } from "@/lib/engine";
import { DynamoDbCapacitySimulator } from "@/components/dashboard/dynamodb-capacity-simulator";
import { useT } from "@/lib/i18n";

export default function SimulatorPage() {
  const t = useT();
  // Global selection (shared store): switching via ⌘K/header persists here.
  const { clusters, selected: selectedCluster } = useSelectedCluster();

  const current = clusters.find((c) => c.cluster_id === selectedCluster);
  // 시뮬레이션(업그레이드/파라미터/DDL/스케일링)은 Aurora 전용: 버전 업그레이드,
  // SQL DDL, DB 파라미터그룹, ACU/인스턴스 리사이즈는 NoSQL 등가물이 없다. DynamoDB는
  // 용량 모드 비용 what-if 전용 패널을 보여주고, DocumentDB는 안내 문구를 보여준다
  // (백엔드 핸들러 가드와 일관: dynamodb는 ddb_cost_simulation 능력만 양성 게이트).
  const fam = engineFamily(current?.engine);

  return (
    <PageBody>
      <PageHeader
        eyebrow={t("자동화")}
        title={t("Simulator")}
        description={t(
          "업그레이드, 파라미터, 스케일링, DDL 영향을 실제 실행 전에 추정합니다. 모든 결과는 추정치이며 프로덕션 적용 전 별도 검증 필수.",
        )}
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
          title={t("클러스터가 없습니다")}
          description={t(
            "시뮬레이션을 실행하려면 Clusters 페이지에서 먼저 등록하세요.",
          )}
        />
      ) : fam === "dynamodb" ? (
        <DynamoDbCapacitySimulator clusterId={selectedCluster} />
      ) : fam === "elasticache" ? (
        <ElasticacheNodeResizePanel clusterId={selectedCluster} />
      ) : fam === "rds_instance" ? (
        <RdsRightsizingSimulator
          clusterId={selectedCluster}
          engine={current?.engine}
        />
      ) : fam !== "relational" ? (
        <EmptyState
          title={t("DocumentDB 시뮬레이션은 지원 예정")}
          description={t(
            "업그레이드, 파라미터, DDL, 스케일링 시뮬레이션은 Aurora PostgreSQL/MySQL 전용입니다. DocumentDB의 용량/비용 권장은 대시보드의 Maintenance Health 패널과 Chat 진단을 참고하세요.",
          )}
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
// Upgrade Wizard: compatibility → impact methods → plan steps
// ---------------------------------------------------------------------------

function UpgradePanel({
  clusterId,
  engine,
}: {
  clusterId: string;
  engine?: string;
}) {
  const t = useT();
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
  // Which method row's time breakdown is expanded, if any.
  const [openMethod, setOpenMethod] = useState<string | null>(null);

  useEffect(() => {
    setCompat(null);
    setImpact(null);
    setPlan(null);
    setErr(null);
    setTarget("");
    setOpenMethod(null);
  }, [clusterId]);

  const run = async () => {
    if (!target.trim()) return;
    setLoading(true);
    setErr(null);
    setOpenMethod(null);
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
      title={t("버전 업그레이드 시뮬레이션")}
      description={t(
        "현재 {engine} 클러스터에서 target 버전으로 업그레이드할 때의 호환성, 메서드별 시간/다운타임/리스크, 단계별 실행 계획을 한 번에 추정합니다.",
      ).replace("{engine}", engine ?? "engine")}
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
            placeholder={t("예: 16.4")}
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
            {loading ? t("추정 중…") : t("시뮬레이션 실행")}
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
                    {t("호환 가능")}
                  </span>
                ) : (
                  <span className="px-1.5 py-0.5 bg-rose-500/15 text-rose-300 border border-rose-500/40 text-[10px]">
                    {t("직접 업그레이드 불가")}
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
                    <span className="text-zinc-500">
                      {t("유효한 직접 대상: ")}
                    </span>
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
            <div className="flex flex-wrap items-center gap-2 mb-2">
              <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                Methods
              </span>
              {impact.upgrade_type && (
                <span className="px-1.5 py-0.5 border text-[10px] text-zinc-300 border-zinc-700 bg-zinc-900/60">
                  {impact.upgrade_type === "major" ? t("메이저") : t("마이너")}
                  {typeof impact.major_jump === "number" &&
                    impact.major_jump > 1 &&
                    ` ${t("{n}단계").replace(
                      "{n}",
                      String(impact.major_jump),
                    )}`}
                </span>
              )}
              {impact.confidence && (
                <ConfidenceBadge confidence={impact.confidence} />
              )}
              <span className="text-[10px] text-zinc-600 font-mono ml-auto">
                storage {fmtDecimal(impact.storage_gb, 0)} GB
                {typeof impact.table_count === "number" &&
                  `, ${fmtDecimal(impact.table_count, 0)} tables`}
                {typeof impact.readers === "number" &&
                  `, readers ${impact.readers}`}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs min-w-[640px]">
                <thead className="text-[10px] uppercase tracking-wider text-zinc-500 border-b border-zinc-800">
                  <tr>
                    <th className="text-left py-1.5 font-medium">Method</th>
                    <th className="text-right py-1.5 font-medium">Est. time</th>
                    <th className="text-right py-1.5 font-medium">Downtime</th>
                    <th className="text-left py-1.5 font-medium pl-3">Risk</th>
                  </tr>
                </thead>
                <tbody>
                  {impact.methods.map((m) => {
                    const open = openMethod === m.method;
                    const hasBasis = !!m.basis && m.basis.length > 0;
                    return (
                      <Fragment key={m.method}>
                        <tr
                          onClick={() =>
                            hasBasis && setOpenMethod(open ? null : m.method)
                          }
                          className={`border-b border-zinc-900 ${
                            hasBasis
                              ? "cursor-pointer hover:bg-zinc-800/40"
                              : ""
                          } ${
                            m.method === impact.recommendation && !open
                              ? "bg-emerald-500/5"
                              : ""
                          }`}
                          title={
                            hasBasis ? t("클릭하여 추정 근거 보기") : undefined
                          }
                        >
                          <td className="py-1.5 font-mono text-zinc-200 align-top">
                            {hasBasis && (
                              <span className="text-zinc-600 mr-1.5 inline-block w-2 select-none">
                                {open ? "▾" : "▸"}
                              </span>
                            )}
                            {m.method}
                            {m.method === impact.recommendation && (
                              <span className="ml-2 text-[10px] text-emerald-400">
                                {t("★ 권장")}
                              </span>
                            )}
                          </td>
                          <td className="py-1.5 text-right font-mono text-zinc-300 tabular-nums align-top">
                            {t("~{n}분").replace(
                              "{n}",
                              String(m.estimated_minutes),
                            )}
                            {typeof m.range_low_minutes === "number" &&
                              typeof m.range_high_minutes === "number" && (
                                <span className="block text-[10px] text-zinc-600">
                                  {t("{a}-{b}분")
                                    .replace("{a}", String(m.range_low_minutes))
                                    .replace(
                                      "{b}",
                                      String(m.range_high_minutes),
                                    )}
                                </span>
                              )}
                          </td>
                          <td className="py-1.5 text-right font-mono text-zinc-300 tabular-nums align-top">
                            {t(m.downtime_text)}
                          </td>
                          <td className="py-1.5 pl-3 align-top">
                            <RiskBadge risk={m.risk} />
                          </td>
                        </tr>
                        {open && hasBasis && (
                          <tr className="border-b border-zinc-900 bg-zinc-950/60">
                            <td colSpan={4} className="px-2 pb-3 pt-1">
                              <div className="border-l-2 border-emerald-500/40 pl-3">
                                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                                  {t("{method} ~{n}분 추정 근거")
                                    .replace("{method}", m.method)
                                    .replace(
                                      "{n}",
                                      String(m.estimated_minutes),
                                    )}
                                </div>
                                <ul className="space-y-1">
                                  {m.basis!.map((b, i) => (
                                    <li
                                      key={i}
                                      className="text-[11px] text-zinc-400 leading-relaxed flex gap-1.5"
                                    >
                                      <span className="text-emerald-500/60 select-none">
                                        -
                                      </span>
                                      <span>{t(b)}</span>
                                    </li>
                                  ))}
                                </ul>
                                {typeof m.range_low_minutes === "number" &&
                                  typeof m.range_high_minutes === "number" && (
                                    <div className="text-[10px] text-zinc-600 mt-1.5">
                                      {t("추정 범위 {a}-{b}분")
                                        .replace(
                                          "{a}",
                                          String(m.range_low_minutes),
                                        )
                                        .replace(
                                          "{b}",
                                          String(m.range_high_minutes),
                                        )}
                                      {impact.confidence &&
                                        t(", 신뢰도 {c}").replace(
                                          "{c}",
                                          t(CONFIDENCE_KO[impact.confidence]),
                                        )}
                                    </div>
                                  )}
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="mt-3 space-y-1 border-t border-zinc-800 pt-2">
              {impact.recommendation_reason && (
                <p className="text-[11px] text-zinc-400 leading-relaxed">
                  <span className="text-emerald-400/80 mr-1">
                    {t("권장 근거")}
                  </span>
                  {t(impact.recommendation_reason)}
                </p>
              )}
              {impact.object_count_basis && (
                <p className="text-[10px] text-zinc-500">
                  {t(impact.object_count_basis)}
                </p>
              )}
              {impact.methodology_note && (
                <p className="text-[10px] text-zinc-600 leading-relaxed">
                  {t(impact.methodology_note)}
                </p>
              )}
            </div>
          </div>
        )}

        {plan && (
          <div className="px-4 py-3">
            <div className="flex items-baseline justify-between mb-2 gap-3 flex-wrap">
              <div className="flex items-center gap-2">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                  Plan: {plan.method}
                </span>
                {plan.confidence && (
                  <ConfidenceBadge confidence={plan.confidence} />
                )}
              </div>
              <div className="text-[10px] text-zinc-500 font-mono text-right">
                {t("~{n}분").replace(
                  "{n}",
                  String(plan.estimated_total_minutes),
                )}
                {plan.estimated_range_minutes && (
                  <span className="text-zinc-600">
                    {" "}
                    {t("({a}-{b}분)")
                      .replace("{a}", String(plan.estimated_range_minutes[0]))
                      .replace("{b}", String(plan.estimated_range_minutes[1]))}
                  </span>
                )}
                {plan.downtime_text && (
                  <span className="block text-zinc-600">
                    {t("다운타임 {n}").replace("{n}", t(plan.downtime_text))}
                  </span>
                )}
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
                  <span className="text-zinc-200 font-medium">
                    {t(s.action)}
                  </span>
                  <span className="text-zinc-400 font-mono text-[11px] break-all">
                    {t(s.details)}
                  </span>
                </li>
              ))}
            </ol>
            <div className="mt-3 text-[11px] text-zinc-400 border-t border-zinc-800 pt-2">
              <span className="text-zinc-500 uppercase tracking-wider text-[10px] mr-2">
                Rollback
              </span>
              {t(plan.rollback_plan)}
            </div>
          </div>
        )}

        {!compat && !loading && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            {t("target 버전을 입력하고")}{" "}
            <span className="text-amber-300">{t("시뮬레이션 실행")}</span>
            {t("을 누르세요.")}
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
  const t = useT();
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
      title={t("파라미터 변경 시뮬레이션")}
      description={t(
        "동적/정적 여부, 재시작 필요 여부, 영향 영역을 즉시 추정합니다.",
      )}
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
            placeholder={t("예: work_mem")}
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
            placeholder={t("예: 16MB")}
            className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-32"
          />
          <button
            onClick={run}
            disabled={loading || !param.trim() || !value.trim()}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
          >
            {loading ? t("추정 중…") : t("시뮬레이션 실행")}
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
            {/* Live current → new value (only when the param group was read). */}
            {(result.current_value !== undefined ||
              result.current_value_note) && (
              <div className="sm:col-span-3 text-[11px] text-zinc-400 flex flex-wrap items-center gap-2">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                  Value
                </span>
                <span className="font-mono text-zinc-300">
                  {result.current_value ?? t("(엔진 기본값)")}
                </span>
                <span className="text-zinc-600">→</span>
                <span className="font-mono text-zinc-200">
                  {result.new_value}
                </span>
                {result.is_modifiable === false && (
                  <span className="px-1.5 py-0.5 border text-[10px] text-rose-300 border-rose-500/40 bg-rose-500/10">
                    {t("수정 불가")}
                  </span>
                )}
                {result.allowed_values && (
                  <span className="text-[10px] text-zinc-500 font-mono">
                    {t("허용: {n}").replace("{n}", result.allowed_values)}
                  </span>
                )}
              </div>
            )}
            {result.valid === false && result.validation_reason && (
              <div className="sm:col-span-3 text-[11px] text-rose-300 border border-rose-500/30 bg-rose-500/5 px-3 py-1.5">
                ⚠ {t(result.validation_reason)}
              </div>
            )}
            <div className="sm:col-span-3 text-zinc-300 border border-zinc-800 bg-zinc-900/60 px-3 py-2">
              <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                Recommendation
              </span>
              {t(result.recommendation)}
              {result.impact_note && (
                <div className="text-[10px] text-zinc-500 mt-1">
                  {t(result.impact_note)}
                </div>
              )}
              <div className="text-[10px] text-zinc-600 mt-1 flex flex-wrap gap-2">
                {result.parameter_group && (
                  <span className="font-mono">
                    pg: {result.parameter_group}
                  </span>
                )}
                {result.data_source && <span>{result.data_source}</span>}
              </div>
              {!result.known && (
                <div className="text-[10px] text-amber-400 mt-1">
                  {t("카탈로그 미등록: 결과는 보수적 기본값입니다")}
                </div>
              )}
            </div>
          </div>
        )}

        {!result && !loading && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            {t("파라미터명과 값을 입력하세요. 등록된")}{" "}
            <span className="text-zinc-300">{catalog.length}</span>
            {t("개의 파라미터에 대해 자동완성이 동작합니다.")}
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
  const t = useT();
  const [minAcu, setMinAcu] = useState<string>("");
  const [maxAcu, setMaxAcu] = useState<string>("");
  const [instanceClass, setInstanceClass] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<ScalingResponse | null>(null);

  // 클러스터 선택 즉시 무변경 베이스라인 시뮬레이션을 자동 실행한다.
  // 이전에는 첫 실행 결과가 와야 mode를 알 수 있어서 프로비저닝 클러스터에도
  // ACU 입력이 먼저 보였다(모드는 백엔드가 describe로 라이브 판별: AWS는
  // Sv2도 EngineMode "provisioned"로 보고하므로 프런트 단독 판별 불가).
  // 베이스라인 결과로 입력 컨트롤이 처음부터 실제 모드를 따르고, 현재
  // 구성과 월 비용도 입력 전에 보인다.
  useEffect(() => {
    setResult(null);
    setErr(null);
    setMinAcu("");
    setMaxAcu("");
    setInstanceClass("");
    let alive = true;
    setLoading(true);
    simulateScaling(clusterId, null, null, null)
      .then((r) => {
        if (alive) setResult(r);
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

  const run = async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await simulateScaling(
        clusterId,
        minAcu === "" ? null : Number(minAcu),
        maxAcu === "" ? null : Number(maxAcu),
        instanceClass.trim() === "" ? null : instanceClass.trim(),
      );
      setResult(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  };

  const mode = result?.mode;
  const provisioned = mode === "provisioned";

  const delta = result?.cost_impact.delta_monthly_usd ?? null;
  const pct = result?.cost_impact.change_pct ?? null;
  const deltaTone =
    delta == null
      ? "text-zinc-300"
      : delta > 0
        ? "text-rose-300"
        : delta < 0
          ? "text-emerald-300"
          : "text-zinc-300";

  return (
    <Section
      eyebrow="Scaling"
      title={t("스케일링 비용 시뮬레이션")}
      description={t(
        "Aurora Serverless v2(ACU min/max) 또는 프로비저닝 인스턴스 클래스를 조정했을 때의 월 비용 변화를, 클러스터 리전과 에디션(I/O-Optimized) 기준 실시간 Pricing 단가로 추정합니다.",
      )}
    >
      <div className="bg-zinc-900/50 border border-zinc-800">
        <div className="px-4 py-3 border-b border-zinc-800 flex flex-wrap items-center gap-3">
          {!result ? (
            <span className="text-[11px] text-zinc-500">
              {err
                ? t("모드 감지 실패. 아래 오류 확인")
                : t("클러스터 모드 감지 중…")}
            </span>
          ) : provisioned ? (
            <>
              <label className="text-[10px] uppercase tracking-wider text-zinc-500">
                New instance class
              </label>
              <input
                type="text"
                value={instanceClass}
                onChange={(e) => setInstanceClass(e.target.value)}
                placeholder={result.current.instance_class || "db.r6g.xlarge"}
                spellCheck={false}
                className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-44"
              />
            </>
          ) : (
            <>
              <label className="text-[10px] uppercase tracking-wider text-zinc-500">
                New min ACU
              </label>
              <input
                type="number"
                step="0.5"
                min="0.5"
                value={minAcu}
                onChange={(e) => setMinAcu(e.target.value)}
                placeholder={String(result.current.min_acu ?? 0.5)}
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
                placeholder={String(result.current.max_acu ?? 4)}
                className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-20 tabular-nums"
              />
            </>
          )}
          <button
            onClick={run}
            disabled={loading || !result}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
          >
            {loading ? t("추정 중…") : t("비용 추정")}
          </button>
        </div>

        {err && <div className="p-4 text-xs text-rose-300">{err}</div>}

        {result && (
          <div className="p-4 space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              {provisioned ? (
                <>
                  <InstanceCard
                    label={t("현재")}
                    instanceClass={result.current.instance_class}
                    monthly={result.cost_impact.current_monthly_usd}
                    tone="zinc"
                  />
                  <InstanceCard
                    label={t("제안")}
                    instanceClass={result.proposed.instance_class}
                    monthly={result.cost_impact.proposed_monthly_usd}
                    tone={delta != null && delta > 0 ? "amber" : "emerald"}
                    deltaPct={pct}
                  />
                </>
              ) : (
                <>
                  <RangeCard
                    label={t("현재")}
                    min={result.current.min_acu}
                    max={result.current.max_acu}
                    monthly={result.cost_impact.current_monthly_usd}
                    tone="zinc"
                  />
                  <RangeCard
                    label={t("제안")}
                    min={result.proposed.min_acu}
                    max={result.proposed.max_acu}
                    monthly={result.cost_impact.proposed_monthly_usd}
                    tone={delta != null && delta > 0 ? "amber" : "emerald"}
                    deltaPct={pct}
                  />
                </>
              )}
            </div>
            <div className="grid gap-2 sm:grid-cols-[1fr_auto] items-baseline border-t border-zinc-800 pt-2.5">
              <div className="text-xs text-zinc-400">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                  {t("월 차액")}
                </span>
                <span className={`font-mono ${deltaTone}`}>
                  {delta == null
                    ? "n/a"
                    : `${delta > 0 ? "+" : ""}$${fmtDecimal(delta, 2)} / month`}
                </span>
              </div>
              <div className="text-[10px] text-zinc-500 font-mono">
                writers {result.writers}, readers {result.readers}
              </div>
            </div>
            {result.mode === "serverless" && result.acu_basis && (
              <div className="flex flex-wrap items-center gap-2 text-[10px]">
                {result.acu_basis === "observed" ? (
                  <span className="px-1.5 py-0.5 border text-emerald-300 border-emerald-500/40 bg-emerald-500/10">
                    {t("관측 ACU {n} 기준").replace(
                      "{n}",
                      fmtDecimal(result.observed_avg_acu ?? 0, 2),
                    )}
                  </span>
                ) : (
                  <span className="px-1.5 py-0.5 border text-amber-300 border-amber-500/40 bg-amber-500/10">
                    {t("중간값 ACU 추정 (관측 데이터 없음)")}
                  </span>
                )}
                {result.confidence && (
                  <ConfidenceBadge confidence={result.confidence} />
                )}
              </div>
            )}
            <PricingContext
              pricing={result.unit_pricing}
              dataSource={result.data_source}
            />
            <div className="text-[11px] text-zinc-500">{t(result.note)}</div>
          </div>
        )}

        {!result && loading && (
          <div className="p-6 text-zinc-500 text-sm">
            {t(
              "현재 구성과 월 비용을 불러오는 중입니다. 클러스터 모드(Serverless v2 / 프로비저닝)에 맞는 입력이 곧 표시됩니다.",
            )}
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
  const t = useT();
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
      title={t("DDL 영향 시뮬레이션")}
      description={t(
        "ALTER / CREATE INDEX 등 DDL을 실행했을 때의 락 타입, 예상 소요 시간, 디스크 추가 사용량을 추정합니다.",
      )}
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
            {t("샘플 채우기")}
          </button>
          <button
            onClick={run}
            disabled={loading || !ddl.trim()}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
          >
            {loading ? t("추정 중…") : t("영향 추정")}
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
            <div className="flex flex-wrap items-center gap-2">
              {result.operation && (
                <span className="px-1.5 py-0.5 border text-[10px] font-mono text-zinc-300 border-zinc-700 bg-zinc-900/60">
                  {result.operation}
                </span>
              )}
              {result.confidence && (
                <ConfidenceBadge confidence={result.confidence} />
              )}
              {typeof result.throughput_mb_s === "number" && (
                <span className="text-[10px] text-zinc-600 font-mono ml-auto">
                  {t("추정 처리량 ~{n} MB/s").replace(
                    "{n}",
                    fmtDecimal(result.throughput_mb_s, 0),
                  )}
                </span>
              )}
            </div>
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
                value={
                  result.estimated_range_seconds
                    ? `~${fmtExact(result.estimated_seconds)} s (${fmtExact(
                        result.estimated_range_seconds[0],
                      )}-${fmtExact(result.estimated_range_seconds[1])})`
                    : `~${fmtExact(result.estimated_seconds)} s`
                }
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
              {t(result.recommendation)}
            </div>
            {result.basis && result.basis.length > 0 && (
              <div className="border-l-2 border-emerald-500/40 pl-3">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  {t("추정 근거")}
                </div>
                <ul className="space-y-0.5">
                  {result.basis.map((b, i) => (
                    <li
                      key={i}
                      className="text-[11px] text-zinc-400 leading-relaxed flex gap-1.5"
                    >
                      <span className="text-emerald-500/60 select-none">-</span>
                      <span>{t(b)}</span>
                    </li>
                  ))}
                </ul>
                {result.note && (
                  <p className="text-[10px] text-zinc-600 leading-relaxed mt-1.5">
                    {t(result.note)}
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        {!result && !loading && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            {t(
              "DDL SQL을 입력하면 테이블 크기 추정과 락/온라인 가능 여부를 분석합니다.",
            )}
          </div>
        )}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// ElastiCache node-resize cost simulator
// ---------------------------------------------------------------------------

function ElasticacheNodeResizePanel({ clusterId }: { clusterId: string }) {
  const t = useT();
  const [nodeType, setNodeType] = useState("");
  const [nodeCount, setNodeCount] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<ElasticacheNodeResizeResponse | null>(
    null,
  );

  useEffect(() => {
    setResult(null);
    setErr(null);
    setNodeType("");
    setNodeCount("");
  }, [clusterId]);

  const run = async () => {
    setLoading(true);
    setErr(null);
    try {
      const opts: { newNodeType?: string; newNodeCount?: number } = {};
      if (nodeType.trim()) opts.newNodeType = nodeType.trim();
      if (nodeCount.trim()) {
        const n = parseInt(nodeCount, 10);
        if (!isNaN(n) && n > 0) opts.newNodeCount = n;
      }
      const r = await simulateElasticacheNodeResize(clusterId, opts);
      setResult(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  };

  const delta = result?.delta_monthly ?? null;
  const deltaTone =
    delta == null
      ? "text-zinc-300"
      : delta > 0
        ? "text-rose-300"
        : delta < 0
          ? "text-emerald-300"
          : "text-zinc-300";

  return (
    <Section
      eyebrow="ElastiCache Cost"
      title={t("노드 리사이즈 비용 시뮬레이션")}
      description={t(
        "ElastiCache 노드 타입과 노드 수를 변경했을 때의 월 비용 변화를 리전별 실시간 AWS Pricing 단가로 추정합니다. 노드-시간 비용만 대상이며 데이터 전송, 스냅샷, 예약 노드는 제외합니다.",
      )}
    >
      <div className="bg-zinc-900/50 border border-zinc-800">
        <div className="px-4 py-3 border-b border-zinc-800 flex flex-wrap items-center gap-3">
          <label className="text-[10px] uppercase tracking-wider text-zinc-500">
            {t("새 노드 타입")}
          </label>
          <input
            type="text"
            value={nodeType}
            onChange={(e) => setNodeType(e.target.value)}
            placeholder={t("예: cache.r7g.large")}
            spellCheck={false}
            className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-44"
          />
          <label className="text-[10px] uppercase tracking-wider text-zinc-500 ml-2">
            {t("노드 수")}
          </label>
          <input
            type="number"
            min="1"
            step="1"
            value={nodeCount}
            onChange={(e) => setNodeCount(e.target.value)}
            placeholder={result ? String(result.current.node_count ?? 1) : "1"}
            className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-20 tabular-nums"
          />
          <button
            onClick={run}
            disabled={loading}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
          >
            {loading ? t("추정 중…") : t("비용 추정")}
          </button>
        </div>

        {err && (
          <div className="p-4 text-xs text-rose-300 border-b border-zinc-800 bg-rose-500/5">
            {err}
          </div>
        )}

        {result && (
          <div className="p-4 space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className={`border px-3 py-2 ${TONE_CLASSES["zinc"]}`}>
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  {t("현재")}
                </div>
                <div className="text-base font-mono break-all">
                  {result.current.node_type || "-"}
                </div>
                <div className="text-[11px] text-zinc-500 mt-0.5 font-mono">
                  {result.current.node_count != null
                    ? t("× {n} 노드").replace(
                        "{n}",
                        fmtExact(result.current.node_count),
                      )
                    : ""}
                </div>
                <div className="text-[11px] text-zinc-400 mt-1 font-mono">
                  {result.current_monthly == null
                    ? "n/a"
                    : `$${fmtDecimal(result.current_monthly, 2)}/mo`}
                </div>
              </div>
              <div
                className={`border px-3 py-2 ${
                  TONE_CLASSES[delta != null && delta > 0 ? "amber" : "emerald"]
                }`}
              >
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  {t("제안")}
                </div>
                <div className="text-base font-mono break-all">
                  {result.proposed.node_type || "-"}
                </div>
                <div className="text-[11px] text-zinc-500 mt-0.5 font-mono">
                  {result.proposed.node_count != null
                    ? t("× {n} 노드").replace(
                        "{n}",
                        fmtExact(result.proposed.node_count),
                      )
                    : ""}
                </div>
                <div className="text-[11px] text-zinc-400 mt-1 font-mono">
                  {result.proposed_monthly == null
                    ? "n/a"
                    : `$${fmtDecimal(result.proposed_monthly, 2)}/mo`}
                  {result.delta_pct != null &&
                    Math.abs(result.delta_pct) > 0.1 && (
                      <span
                        className={`ml-2 ${
                          result.delta_pct > 0
                            ? "text-rose-300"
                            : "text-emerald-300"
                        }`}
                      >
                        ({result.delta_pct > 0 ? "+" : ""}
                        {fmtDecimal(result.delta_pct, 1)}%)
                      </span>
                    )}
                </div>
              </div>
            </div>

            <div className="grid gap-2 sm:grid-cols-[1fr_auto] items-baseline border-t border-zinc-800 pt-2.5">
              <div className="text-xs text-zinc-400">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                  {t("월 차액")}
                </span>
                <span className={`font-mono ${deltaTone}`}>
                  {delta == null
                    ? "n/a"
                    : `${delta > 0 ? "+" : ""}$${fmtDecimal(delta, 2)} / month`}
                </span>
              </div>
              <div className="text-[10px] text-zinc-500 font-mono">
                {result.region && <span>{result.region}</span>}
              </div>
            </div>

            {result.status === "partial" && (
              <div className="flex items-center gap-2 text-[10px]">
                <span className="px-1.5 py-0.5 border text-amber-300 border-amber-500/40 bg-amber-500/10">
                  {t("부분 추정: 일부 단가 미조회")}
                </span>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-zinc-500 font-mono border-t border-zinc-800 pt-2.5">
              <span
                className={`px-1.5 py-0.5 border ${
                  result.pricing_source === "aws_pricing_api"
                    ? "text-emerald-300 border-emerald-500/40 bg-emerald-500/5"
                    : "text-amber-300 border-amber-500/40 bg-amber-500/5"
                }`}
              >
                {result.pricing_source === "aws_pricing_api"
                  ? "Pricing API"
                  : "fallback"}
              </span>
              {result.current.price_per_hour != null && (
                <span>
                  {t("현재 ${n}/hr/node").replace(
                    "{n}",
                    fmtDecimal(result.current.price_per_hour, 4),
                  )}
                </span>
              )}
              {result.proposed.price_per_hour != null &&
                result.proposed.node_type !== result.current.node_type && (
                  <span>
                    {t("제안 ${n}/hr/node").replace(
                      "{n}",
                      fmtDecimal(result.proposed.price_per_hour, 4),
                    )}
                  </span>
                )}
            </div>

            {result.note && (
              <div className="text-[11px] text-zinc-500">{t(result.note)}</div>
            )}
          </div>
        )}

        {!result && !loading && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            {t("노드 타입 또는 노드 수를 입력하고")}{" "}
            <span className="text-amber-300">{t("비용 추정")}</span>
            {t(
              "을 누르세요. 입력 없이 실행하면 현재 구성 기준 월 비용을 조회합니다.",
            )}
          </div>
        )}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// RDS instance (MySQL/SQL Server, non-Aurora) right-sizing + cost simulator.
// Backed by the REST route POST /api/simulation/rds-instance-rightsizing (the
// api/simulation Lambda replicates the pricing/cost logic), matching every
// sibling cost-sim panel: exact AWS Price List numbers with no LLM relay.
// RdsRightsizingResponse is defined in api-client.ts.
// ---------------------------------------------------------------------------

const RDS_ACTION_KO: Record<string, string> = {
  downsize: "다운사이즈 권장",
  upsize: "업사이즈 권장",
  hold: "현재 유지",
};

function RdsActionBadge({ action }: { action?: string }) {
  const t = useT();
  const tone =
    action === "downsize"
      ? "text-emerald-300 border-emerald-500/40 bg-emerald-500/10"
      : action === "upsize"
        ? "text-amber-300 border-amber-500/40 bg-amber-500/10"
        : "text-zinc-400 border-zinc-700 bg-zinc-900/40";
  return (
    <span
      className={`px-1.5 py-0.5 border text-[10px] font-mono uppercase tracking-wider whitespace-nowrap ${tone}`}
    >
      {action ? t(RDS_ACTION_KO[action] ?? action) : "-"}
    </span>
  );
}

function RdsRightsizingSimulator({
  clusterId,
  engine,
}: {
  clusterId: string;
  engine?: string;
}) {
  const t = useT();
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<RdsRightsizingResponse | null>(null);

  const run = () => {
    setResult(null);
    setErr(null);
    setLoading(true);
    let cancelled = false;
    simulateRdsInstanceRightsizing(clusterId)
      .then((res) => {
        if (cancelled) return;
        setResult(res);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setErr(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  };

  useEffect(() => {
    const cleanup = run();
    return cleanup;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterId]);

  const delta = result?.cost_impact?.delta_monthly_usd ?? null;
  const deltaTone =
    delta == null
      ? "text-zinc-300"
      : delta > 0
        ? "text-rose-300"
        : delta < 0
          ? "text-emerald-300"
          : "text-zinc-300";

  return (
    <Section
      eyebrow="RDS Instance Cost"
      title={t("인스턴스 라이트사이징과 비용 시뮬레이션")}
      description={t(
        "최근 CloudWatch 사용률(CPU, 연결, IOPS)을 기준으로 인스턴스 클래스 적정성을 진단하고, AWS Price List 실시간 단가로 현재 대비 제안 인스턴스의 월 비용을 추정합니다.",
      )}
    >
      <div className="bg-zinc-900/50 border border-zinc-800">
        <div className="px-4 py-3 border-b border-zinc-800 flex flex-wrap items-center gap-3">
          <span className="text-[10px] uppercase tracking-wider text-zinc-500">
            Engine
          </span>
          <span className="text-xs font-mono text-zinc-300">
            {engine || "-"}
          </span>
          <button
            onClick={run}
            disabled={loading}
            className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
          >
            {loading ? t("분석 중…") : result ? t("다시 계산") : t("지금 계산")}
          </button>
        </div>

        {err && (
          <div className="p-4 text-xs text-rose-300 border-b border-zinc-800 bg-rose-500/5">
            {err}
          </div>
        )}

        {loading && !result && !err && (
          <div className="p-6 text-zinc-500 text-sm">
            {t("CloudWatch 사용률과 AWS Price List 단가를 조회하는 중입니다…")}
          </div>
        )}

        {!loading && result && result.status === "insufficient_data" && (
          <div className="p-4">
            <EmptyState
              eyebrow={t("데이터 부족")}
              title={t("사용률 데이터가 충분하지 않습니다")}
              description={
                result.message ??
                t(
                  "right-sizing 권장을 산출하려면 CloudWatch 사용률 데이터가 더 필요합니다.",
                )
              }
            />
          </div>
        )}

        {!loading &&
          result &&
          (result.status === "error" ||
            result.status === "unsupported_engine") && (
            <div className="p-4 text-xs text-rose-300">
              {t(
                result.message ?? result.reason ?? "시뮬레이션에 실패했습니다.",
              )}
            </div>
          )}

        {!loading && result && result.status === "ok" && (
          <div className="p-4 space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-500 font-mono">
              <span className="px-1.5 py-0.5 border text-zinc-300 border-zinc-700 bg-zinc-900/60">
                {result.current?.instance_class || "-"}
              </span>
              {result.current?.storage_gb != null && (
                <span>
                  {fmtDecimal(result.current.storage_gb, 0)} GB{" "}
                  {result.current.storage_type || ""}
                </span>
              )}
              {result.current?.iops != null && (
                <span>{fmtDecimal(result.current.iops, 0)} IOPS</span>
              )}
              {result.region && (
                <span className="ml-auto">{result.region}</span>
              )}
            </div>

            <StatRow cols={3}>
              <Stat
                label="CPU p95"
                value={
                  result.utilization?.cpu_p95 != null
                    ? `${fmtDecimal(result.utilization.cpu_p95, 1)}%`
                    : "n/a"
                }
                hint={
                  result.utilization?.window_hours != null
                    ? t("{n}h 윈도우").replace(
                        "{n}",
                        fmtDecimal(result.utilization.window_hours, 0),
                      )
                    : undefined
                }
              />
              <Stat
                label={t("피크 연결 수")}
                value={
                  result.utilization?.conn_peak != null
                    ? fmtDecimal(result.utilization.conn_peak, 0)
                    : "n/a"
                }
              />
              <Stat
                label="Read/Write IOPS p95"
                value={
                  result.utilization?.read_iops_p95 != null ||
                  result.utilization?.write_iops_p95 != null
                    ? `${fmtDecimal(
                        result.utilization?.read_iops_p95,
                        0,
                      )} / ${fmtDecimal(result.utilization?.write_iops_p95, 0)}`
                    : "n/a"
                }
              />
            </StatRow>

            <div className="border border-zinc-800 bg-zinc-950/60 px-4 py-3 flex items-start gap-3">
              <RdsActionBadge action={result.recommendation?.action} />
              <div className="text-xs text-zinc-300 leading-relaxed">
                {result.recommendation?.instance_class &&
                  result.recommendation.instance_class !==
                    result.current?.instance_class && (
                    <span className="font-mono text-zinc-200 mr-1">
                      → {result.recommendation.instance_class}
                    </span>
                  )}
                {result.recommendation?.reason &&
                  t(result.recommendation.reason)}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className={`border px-3 py-2 ${TONE_CLASSES["zinc"]}`}>
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  {t("현재")}
                </div>
                <div className="text-base font-mono break-all">
                  {result.current?.instance_class || "-"}
                </div>
                <MonthlyLine
                  monthly={result.cost_impact?.current_monthly_usd}
                />
              </div>
              <div
                className={`border px-3 py-2 ${
                  TONE_CLASSES[delta != null && delta > 0 ? "amber" : "emerald"]
                }`}
              >
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  {t("제안")}
                </div>
                <div className="text-base font-mono break-all">
                  {result.recommendation?.instance_class ||
                    result.current?.instance_class ||
                    "-"}
                </div>
                <MonthlyLine
                  monthly={result.cost_impact?.proposed_monthly_usd}
                  deltaPct={result.cost_impact?.change_pct}
                />
              </div>
            </div>

            <div className="grid gap-2 sm:grid-cols-[1fr_auto] items-baseline border-t border-zinc-800 pt-2.5">
              <div className="text-xs text-zinc-400">
                <span className="text-[10px] uppercase tracking-wider text-zinc-500 mr-2">
                  {t("월 차액")}
                </span>
                <span className={`font-mono ${deltaTone}`}>
                  {delta == null
                    ? "n/a"
                    : `${delta > 0 ? "+" : ""}$${fmtDecimal(delta, 2)} / month`}
                </span>
              </div>
            </div>

            {result.cost_impact?.breakdown?.license_note && (
              <div className="text-[11px] text-zinc-500">
                {t(result.cost_impact.breakdown.license_note)}
              </div>
            )}

            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-zinc-500 font-mono border-t border-zinc-800 pt-2.5">
              <span
                className={`px-1.5 py-0.5 border ${
                  result.cost_impact?.pricing_source === "aws_price_list"
                    ? "text-emerald-300 border-emerald-500/40 bg-emerald-500/5"
                    : "text-amber-300 border-amber-500/40 bg-amber-500/5"
                }`}
              >
                {result.cost_impact?.pricing_source === "aws_price_list"
                  ? "Price List API"
                  : "fallback"}
              </span>
            </div>

            {result.cost_impact?.pricing_source === "fallback_estimate" && (
              <div className="text-[11px] text-amber-300/80">
                {t("실시간 가격을 가져오지 못해 추정치입니다.")}
              </div>
            )}
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

const CONFIDENCE_KO: Record<"low" | "medium" | "high", string> = {
  high: "높음",
  medium: "보통",
  low: "낮음",
};

function ConfidenceBadge({
  confidence,
}: {
  confidence: "low" | "medium" | "high";
}) {
  const t = useT();
  const map = {
    high: {
      tone: "text-emerald-300 border-emerald-500/40 bg-emerald-500/10",
      label: "신뢰도 높음",
    },
    medium: {
      tone: "text-sky-300 border-sky-500/40 bg-sky-500/10",
      label: "신뢰도 보통",
    },
    low: {
      tone: "text-amber-300 border-amber-500/40 bg-amber-500/10",
      label: "신뢰도 낮음",
    },
  } as const;
  const { tone, label } = map[confidence];
  return (
    <span className={`px-1.5 py-0.5 border text-[10px] ${tone}`}>
      {t(label)}
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

/** Shared monthly-cost line for the scaling cards. Renders "n/a" when the
 *  Pricing API could not resolve a unit price (cost field is null). */
function MonthlyLine({
  monthly,
  deltaPct,
}: {
  monthly: number | null | undefined;
  deltaPct?: number | null;
}) {
  return (
    <div className="text-[11px] text-zinc-400 mt-1 font-mono">
      {monthly == null ? "n/a" : `$${fmtDecimal(monthly, 2)}/mo`}
      {deltaPct != null && Math.abs(deltaPct) > 0.1 && (
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
  min: number | undefined;
  max: number | undefined;
  monthly: number | null | undefined;
  tone: Tone;
  deltaPct?: number | null;
}) {
  return (
    <div className={`border px-3 py-2 ${TONE_CLASSES[tone]}`}>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
        {label}
      </div>
      <div className="text-base font-mono tabular-nums">
        {fmtDecimal(min ?? 0, 1)}
        <span className="text-zinc-600 mx-1">→</span>
        {fmtDecimal(max ?? 0, 1)}
        <span className="text-[10px] text-zinc-500 ml-1">ACU</span>
      </div>
      <MonthlyLine monthly={monthly} deltaPct={deltaPct} />
    </div>
  );
}

function InstanceCard({
  label,
  instanceClass,
  monthly,
  tone,
  deltaPct,
}: {
  label: string;
  instanceClass: string | undefined;
  monthly: number | null | undefined;
  tone: Tone;
  deltaPct?: number | null;
}) {
  return (
    <div className={`border px-3 py-2 ${TONE_CLASSES[tone]}`}>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
        {label}
      </div>
      <div className="text-base font-mono break-all">
        {instanceClass || "-"}
      </div>
      <MonthlyLine monthly={monthly} deltaPct={deltaPct} />
    </div>
  );
}

/** Small line under the cost cards making the price provenance explicit: a
 *  live region-aware Pricing API number reads differently from a fallback
 *  estimate, and a DBA needs to know which one they're looking at. */
function PricingContext({
  pricing,
  dataSource,
}: {
  pricing: ScalingUnitPricing;
  dataSource: string;
}) {
  const live = pricing.source === "aws_pricing_api";
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-zinc-500 font-mono border-t border-zinc-800 pt-2.5">
      <span
        className={`px-1.5 py-0.5 border ${
          live
            ? "text-emerald-300 border-emerald-500/40 bg-emerald-500/5"
            : "text-amber-300 border-amber-500/40 bg-amber-500/5"
        }`}
      >
        {live ? "Pricing API" : "fallback"}
      </span>
      <span>
        {pricing.price_per_hour == null
          ? "no unit price"
          : `$${fmtDecimal(pricing.price_per_hour, 4)}/hr, ${pricing.kind}`}
      </span>
      <span>{pricing.region}</span>
      {pricing.io_optimized && <span>I/O-Optimized</span>}
      <span className="text-zinc-600">{dataSource}</span>
    </div>
  );
}
