"use client";

import { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import {
  fetchClusters,
  registerCluster,
  discoverClusters,
  bulkRegisterClusters,
  generateSampleCluster,
  deleteCluster,
  testClusterConnection,
  type DiscoveredCluster,
  type TestConnectionResult,
} from "@/lib/api-client";
import { isAdmin } from "@/lib/auth";
import {
  PageHeader,
  PageBody,
  EmptyState,
  Section,
} from "@/components/design-system/page-shell";
import { SetupGuideModal } from "@/components/clusters/setup-guide-modal";
import { FAMILY_META } from "@/lib/engine";
import {
  groupByEngineFamily,
  displayName,
  FAMILY_ORDER,
} from "@/lib/group-by-family";

interface Cluster {
  cluster_id: string;
  account_id: string;
  region: string;
  engine?: string;
  status?: string;
  engine_version?: string;
  storage_size_gb?: number | string;
  spoke_role_arn?: string;
  connection_status?: string;
  connection_error?: string;
  connection_validated_at?: string;
  registered_at?: string;
  is_demo?: boolean;
  resource_name?: string;
  // ETL freshness — derived from MAX(ts) in metric_snapshots for this
  // cluster. "fresh" within 15min, "stale" older, "no_data" never
  // collected. Useful for spotting ETL pipeline failures per-cluster.
  etl_status?: "fresh" | "stale" | "no_data" | "unknown";
  etl_latest_ts?: string | null;
  etl_rows_24h?: number;
}

const CONN_STYLES: Record<string, { label: string; classes: string }> = {
  ok: {
    label: "ok",
    classes: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  },
  failed: {
    label: "failed",
    classes: "bg-rose-500/10 text-rose-400 border-rose-500/30",
  },
  untested: {
    label: "untested",
    classes: "bg-zinc-700/40 text-zinc-400 border-zinc-700",
  },
};

const STATUS_STYLES: Record<string, string> = {
  available: "text-emerald-400",
  "backing-up": "text-amber-400",
  modifying: "text-amber-400",
  stopped: "text-rose-400",
  failed: "text-rose-400",
};

function EtlBadge({
  status,
  latestTs,
  rows,
}: {
  status?: Cluster["etl_status"];
  latestTs?: string | null;
  rows?: number;
}) {
  if (!status || status === "unknown") {
    return <span className="text-zinc-600 text-[10px] font-mono">—</span>;
  }
  const map: Record<
    NonNullable<Cluster["etl_status"]>,
    {
      label: string;
      classes: string;
      title: (ts?: string | null, n?: number) => string;
    }
  > = {
    fresh: {
      label: "fresh",
      classes: "bg-emerald-500/10 text-emerald-300 border-emerald-500/40",
      title: (ts, n) =>
        ts
          ? `latest snapshot ${new Date(ts).toLocaleString()} · ${
              n ?? 0
            } rows in 24h`
          : "metrics current",
    },
    stale: {
      label: "stale",
      classes: "bg-amber-500/10 text-amber-300 border-amber-500/40",
      title: (ts, n) =>
        ts
          ? `last metric ${new Date(
              ts,
            ).toLocaleString()} — ETL has not committed in 15+ minutes (${
              n ?? 0
            } rows in 24h)`
          : "metric stream stale",
    },
    no_data: {
      label: "no data",
      classes: "bg-rose-500/10 text-rose-300 border-rose-500/40",
      title: () =>
        "metric_snapshots has no rows for this cluster — check the ETL collector logs and the cluster registration",
    },
    unknown: {
      label: "?",
      classes: "bg-zinc-700/40 text-zinc-400 border-zinc-700",
      title: () => "ETL freshness could not be determined",
    },
  };
  const m = map[status];
  return (
    <span
      className={`px-1.5 py-0.5 border text-[10px] font-mono ${m.classes}`}
      title={m.title(latestTs, rows)}
    >
      {m.label}
    </span>
  );
}

function relTime(iso?: string): string {
  if (!iso) return "—";
  // registered_at은 naive UTC isoformat으로 저장돼 왔다(시간대 표기 없음).
  // 그대로 파싱하면 로컬로 해석돼 KST에서 +9h 오차 — 미표기면 Z를 붙인다.
  const norm = /Z$|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso + "Z";
  const diff = Date.now() - new Date(norm).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

export default function ClustersPage() {
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [showForm, setShowForm] = useState(false);
  // Two registration paths: same-account (DBOps control plane and the
  // target Aurora live in the same AWS account → no spoke role needed)
  // and cross-account (target lives elsewhere → STS AssumeRole path).
  // Visualizing these as a single flat form confused first-time DBAs
  // who didn't know whether to fill in spoke_role_arn or not.
  const [registerMode, setRegisterMode] = useState<
    "same-account" | "cross-account"
  >("same-account");
  const [form, setForm] = useState({
    cluster_id: "",
    account_id: "",
    region: "ap-northeast-2",
    engine: "aurora-postgresql",
    spoke_role_arn: "",
  });
  const [submitting, setSubmitting] = useState(false);
  // Pre-flight: separate state from feedback because we want both
  // the per-step verdict (panel below the form) AND the toast at top.
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(
    null,
  );
  const [feedback, setFeedback] = useState<{
    kind: "ok" | "warn" | "err";
    msg: string;
  } | null>(null);
  const [seedingSample, setSeedingSample] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [setupGuideOpen, setSetupGuideOpen] = useState(false);

  const [admin, setAdmin] = useState(false);
  useEffect(() => {
    setAdmin(isAdmin());
  }, []);

  // --- Bulk discovery state (P2.5) ---
  const [discoverOpen, setDiscoverOpen] = useState(false);
  const [discoverForm, setDiscoverForm] = useState({
    regions: "ap-northeast-2",
    role_arn: "",
    account_id: "",
  });
  const [discovering, setDiscovering] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredCluster[]>([]);
  const [discoverErrors, setDiscoverErrors] = useState<Record<string, string>>(
    {},
  );
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [showConfirm, setShowConfirm] = useState(false);
  const [registering, setRegistering] = useState(false);

  // 조회 실패를 빈 배열로 삼키면 기존 등록 클러스터가 사라진 것처럼 보인다 —
  // 에러를 명시적으로 잡아 "0개"와 "조회 실패"를 구분한다(Codex 감사).
  const [clustersError, setClustersError] = useState<string | null>(null);
  const loadClusters = useCallback(() => {
    setClustersError(null);
    fetchClusters()
      .then(setClusters)
      .catch((e) =>
        setClustersError(e instanceof Error ? e.message : "클러스터 조회 실패"),
      );
  }, []);

  useEffect(() => {
    loadClusters();
  }, [loadClusters]);

  const handleDiscover = async () => {
    const regions = discoverForm.regions
      .split(",")
      .map((r) => r.trim())
      .filter(Boolean);
    if (regions.length === 0) {
      setFeedback({ kind: "err", msg: "최소 1개 region이 필요합니다." });
      return;
    }
    setDiscovering(true);
    setFeedback(null);
    setDiscovered([]);
    setDiscoverErrors({});
    setSelectedIds(new Set());
    try {
      const res = await discoverClusters({
        regions,
        role_arn: discoverForm.role_arn.trim() || undefined,
        account_id: discoverForm.account_id.trim() || undefined,
      });
      setDiscovered(res.clusters);
      setDiscoverErrors(res.errors || {});
      // Auto-select unregistered clusters — DBOps 내부 캐시 DB(is_internal)는
      // 제외한다. 자기 자신을 모니터링 대상으로 실수 등록하는 것 방지;
      // 수동 체크로는 여전히 선택 가능(의도적 등록은 막지 않는다).
      setSelectedIds(
        new Set(
          res.clusters
            .filter((c) => !c.already_registered && !c.is_internal)
            .map((c) => c.cluster_id),
        ),
      );
      if (
        res.clusters.length === 0 &&
        Object.keys(res.errors || {}).length === 0
      ) {
        setFeedback({
          kind: "warn",
          msg: "검색된 Aurora 클러스터가 없습니다.",
        });
      }
    } catch (e) {
      setFeedback({
        kind: "err",
        msg: e instanceof Error ? e.message : "Discover failed",
      });
    } finally {
      setDiscovering(false);
    }
  };

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectedClusters = discovered.filter(
    (c) => selectedIds.has(c.cluster_id) && !c.already_registered,
  );

  const handleBulkRegister = async () => {
    if (selectedClusters.length === 0) return;
    setRegistering(true);
    setFeedback(null);
    try {
      const payload = selectedClusters.map((c) => ({
        cluster_id: c.cluster_id,
        cluster_arn: c.cluster_arn,
        account_id: c.account_id || discoverForm.account_id,
        region: c.region,
        engine: c.engine,
        engine_version: c.engine_version,
        endpoint: c.endpoint,
        secret_arn: c.secret_arn,
        db_name: c.db_name,
        spoke_role_arn: discoverForm.role_arn.trim() || undefined,
      }));
      const res = await bulkRegisterClusters(payload);
      const ok = res.counts.registered;
      const skip = res.counts.skipped;
      const fail = res.counts.failed;
      const tone = fail > 0 ? "warn" : "ok";
      setFeedback({
        kind: tone as "ok" | "warn",
        msg: `등록 ${ok}개, 스킵 ${skip}개, 실패 ${fail}개${
          fail > 0
            ? ` — 실패: ${res.failed.map((f) => f.cluster_id).join(", ")}`
            : ""
        }`,
      });
      setShowConfirm(false);
      setDiscoverOpen(false);
      setDiscovered([]);
      setSelectedIds(new Set());
      loadClusters();
    } catch (e) {
      setFeedback({
        kind: "err",
        msg: e instanceof Error ? e.message : "일괄 등록에 실패했습니다",
      });
    } finally {
      setRegistering(false);
    }
  };

  const handleRegister = async () => {
    if (!form.cluster_id || !form.account_id || !form.region) {
      setFeedback({
        kind: "err",
        msg: "cluster_id / account_id / region 모두 필요합니다.",
      });
      return;
    }
    setSubmitting(true);
    setFeedback(null);
    try {
      const result = await registerCluster(form);
      const status = result?.connection_status;
      if (status === "ok") {
        setFeedback({
          kind: "ok",
          msg: `등록됨: ${form.cluster_id} · 연결 검증 통과`,
        });
      } else if (status === "failed") {
        setFeedback({
          kind: "warn",
          msg: `등록은 됐지만 연결 검증 실패: ${
            result?.connection_error || "AWS console 확인"
          }`,
        });
      } else {
        setFeedback({ kind: "ok", msg: `등록됨: ${form.cluster_id}` });
      }
      setShowForm(false);
      setForm({
        cluster_id: "",
        account_id: "",
        region: "ap-northeast-2",
        engine: "aurora-postgresql",
        spoke_role_arn: "",
      });
      loadClusters();
    } catch (e) {
      setFeedback({
        kind: "err",
        msg: e instanceof Error ? e.message : "Failed",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const handleGenerateSample = async () => {
    if (seedingSample) return;
    const proceed = window.confirm(
      "데모용 sample-cluster를 생성합니다. 24시간치 합성 메트릭/쿼리/이상 징후가 캐시 DB에 채워지고, 모든 페이지에서 DEMO 배지로 식별됩니다. 진행할까요?",
    );
    if (!proceed) return;
    setSeedingSample(true);
    setFeedback(null);
    try {
      const res = await generateSampleCluster();
      const total = Object.values(res.rows || {}).reduce((s, n) => s + n, 0);
      setFeedback({
        kind: "ok",
        msg: `Sample 클러스터가 준비됐습니다 (${total.toLocaleString()} 행 시드). Dashboard에서 sample-cluster를 선택해 확인하세요.`,
      });
      loadClusters();
    } catch (e) {
      setFeedback({
        kind: "err",
        msg: e instanceof Error ? e.message : "샘플 생성에 실패했습니다",
      });
    } finally {
      setSeedingSample(false);
    }
  };

  const handleDelete = async (c: Cluster) => {
    if (deletingId) return;
    const ok = window.confirm(
      c.is_demo
        ? `데모 클러스터 ${c.cluster_id} 및 합성 데이터 전체를 삭제합니다.`
        : `${c.cluster_id}를 레지스트리에서 해제합니다. 캐시 DB의 과거 메트릭은 그대로 남습니다.`,
    );
    if (!ok) return;
    setDeletingId(c.cluster_id);
    setFeedback(null);
    try {
      await deleteCluster(c.cluster_id);
      setFeedback({ kind: "ok", msg: `${c.cluster_id} 삭제됨.` });
      loadClusters();
    } catch (e) {
      setFeedback({
        kind: "err",
        msg: e instanceof Error ? e.message : "Delete failed",
      });
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <PageBody>
      <PageHeader
        eyebrow="설정"
        title="클러스터 레지스트리"
        description="Aurora 클러스터 등록과 cross-account 연결 관리. 메트릭/실시간 상태는 Fleet 또는 Dashboard에서 확인하세요."
        actions={
          <>
            <Link
              href="/fleet"
              className="text-xs px-3 py-2 border border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
            >
              Fleet 전체 보기 →
            </Link>
            <button
              onClick={() => setSetupGuideOpen(true)}
              className="text-xs px-3 py-2 border border-amber-500/40 text-amber-300 hover:bg-amber-500/10 transition-colors"
              title="DBOps 전용 read-only 계정 + Secrets Manager 등록 가이드"
            >
              📋 설정 가이드
            </button>
            {admin && (
              <button
                onClick={handleGenerateSample}
                disabled={seedingSample}
                className="text-xs px-3 py-2 border border-purple-500/50 text-purple-300 hover:bg-purple-500/10 disabled:opacity-50 transition-colors"
                title="합성 데이터로 sample-cluster 생성"
              >
                {seedingSample ? "생성 중…" : "🎲 샘플 생성"}
              </button>
            )}
            {admin && (
              <button
                onClick={() => {
                  setDiscoverOpen((v) => !v);
                  setShowForm(false);
                }}
                className="text-xs px-3 py-2 border border-sky-500/50 text-sky-300 hover:bg-sky-500/10 transition-colors"
              >
                {discoverOpen ? "탐색 닫기" : "🔎 클러스터 자동 탐색"}
              </button>
            )}
            {admin && (
              <button
                onClick={() => {
                  setShowForm(!showForm);
                  setDiscoverOpen(false);
                }}
                className="text-xs font-medium px-3 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
              >
                {showForm ? "취소" : "+ Register cluster"}
              </button>
            )}
            {!admin && (
              <span className="text-[10px] uppercase tracking-wider text-zinc-500 px-2 py-1 border border-zinc-800">
                viewer · 읽기 전용
              </span>
            )}
          </>
        }
      />

      {feedback && (
        <div
          className={`mb-6 px-4 py-3 border text-sm ${
            feedback.kind === "ok"
              ? "bg-emerald-500/10 text-emerald-300 border-emerald-500/30"
              : feedback.kind === "warn"
                ? "bg-amber-500/10 text-amber-300 border-amber-500/30"
                : "bg-rose-500/10 text-rose-300 border-rose-500/30"
          }`}
        >
          {feedback.msg}
        </div>
      )}

      {discoverOpen && (
        <Section
          eyebrow="일괄 탐색"
          title="Aurora 클러스터 자동 탐색"
          description="현재 계정 또는 cross-account role을 통해 RDS에서 Aurora 클러스터를 자동 enumerate. 선택한 항목만 한 번에 등록합니다."
        >
          <div className="border border-zinc-800 bg-zinc-900/40 p-6">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <Field
                label="Regions (comma-separated)"
                value={discoverForm.regions}
                onChange={(v) =>
                  setDiscoverForm({ ...discoverForm, regions: v })
                }
                placeholder="ap-northeast-2,us-east-1"
                mono
              />
              <Field
                label="Cross-account role ARN (optional)"
                value={discoverForm.role_arn}
                onChange={(v) =>
                  setDiscoverForm({ ...discoverForm, role_arn: v })
                }
                placeholder="arn:aws:iam::<account>:role/dbops-spoke-role"
                mono
              />
              <Field
                label="Account ID (label only)"
                value={discoverForm.account_id}
                onChange={(v) =>
                  setDiscoverForm({ ...discoverForm, account_id: v })
                }
                placeholder="123456789012"
                mono
              />
            </div>
            <p className="text-[11px] text-zinc-500 mt-3 leading-relaxed">
              role ARN을 비우면 DBOps Lambda의 IAM role로 same-account에서 직접
              조회합니다. cross-account의 경우 해당 role이{" "}
              <span className="font-mono text-zinc-400">
                rds:DescribeDBClusters
              </span>{" "}
              권한을 가져야 합니다.
            </p>
            <div className="mt-5 flex gap-2">
              <button
                onClick={handleDiscover}
                disabled={discovering}
                className="text-xs font-medium px-4 py-2 bg-sky-500 text-zinc-950 hover:bg-sky-400 disabled:opacity-50 transition-colors"
              >
                {discovering ? "검색 중…" : "🔍 Run discovery"}
              </button>
              {discovered.length > 0 && (
                <button
                  onClick={() => setShowConfirm(true)}
                  disabled={selectedClusters.length === 0}
                  className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:bg-zinc-700 disabled:text-zinc-500 transition-colors"
                >
                  Register selected ({selectedClusters.length})
                </button>
              )}
            </div>

            {Object.keys(discoverErrors).length > 0 && (
              <div className="mt-3 text-xs text-rose-400 border border-rose-500/40 bg-rose-500/10 px-3 py-2 space-y-0.5">
                {Object.entries(discoverErrors).map(([r, e]) => (
                  <div key={r}>
                    <span className="font-mono">{r}:</span> {e}
                  </div>
                ))}
              </div>
            )}

            {discovered.length > 0 && (
              <div className="mt-5 border border-zinc-800 overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-zinc-900/60 text-[10px] uppercase tracking-wider text-zinc-500">
                    <tr>
                      <th className="px-3 py-2 w-8">
                        <input
                          type="checkbox"
                          checked={
                            selectedClusters.length > 0 &&
                            selectedClusters.length ===
                              discovered.filter((c) => !c.already_registered)
                                .length
                          }
                          onChange={(e) => {
                            if (e.target.checked) {
                              setSelectedIds(
                                new Set(
                                  discovered
                                    .filter((c) => !c.already_registered)
                                    .map((c) => c.cluster_id),
                                ),
                              );
                            } else {
                              setSelectedIds(new Set());
                            }
                          }}
                          className="accent-amber-500"
                        />
                      </th>
                      <th className="text-left px-3 py-2 font-medium">
                        cluster_id
                      </th>
                      <th className="text-left px-3 py-2 font-medium">
                        engine
                      </th>
                      <th className="text-left px-3 py-2 font-medium">
                        region
                      </th>
                      <th className="text-left px-3 py-2 font-medium">
                        status
                      </th>
                      <th className="text-left px-3 py-2 font-medium">
                        endpoint
                      </th>
                      <th className="text-left px-3 py-2 font-medium">
                        secret
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-800">
                    {discovered.map((c) => (
                      <tr
                        key={`${c.region}:${c.cluster_id}`}
                        className={`hover:bg-zinc-900/40 ${
                          c.already_registered ? "opacity-50" : ""
                        }`}
                      >
                        <td className="px-3 py-2 text-center">
                          <input
                            type="checkbox"
                            checked={
                              selectedIds.has(c.cluster_id) &&
                              !c.already_registered
                            }
                            disabled={c.already_registered}
                            onChange={() => toggleSelect(c.cluster_id)}
                            className="accent-amber-500"
                          />
                        </td>
                        <td className="px-3 py-2 font-mono text-xs text-zinc-100">
                          {c.cluster_id}
                          {c.already_registered && (
                            <span className="ml-2 text-[10px] text-zinc-500">
                              already registered
                            </span>
                          )}
                          {c.is_internal && !c.already_registered && (
                            <span
                              className="ml-2 px-1.5 py-0.5 border border-sky-500/40 bg-sky-500/10 text-sky-300 text-[10px]"
                              title="DBOps 플랫폼 자체의 캐시 DB입니다 — 모니터링 대상으로 등록할 필요가 없어 자동 선택에서 제외했습니다."
                            >
                              DBOps 내부
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2 text-xs">
                          <div className="text-zinc-300">{c.engine}</div>
                          <div className="text-[10px] text-zinc-500 font-mono">
                            {c.engine_version}
                          </div>
                        </td>
                        <td className="px-3 py-2 text-xs font-mono text-zinc-400">
                          {c.region}
                        </td>
                        <td
                          className={`px-3 py-2 text-xs ${
                            STATUS_STYLES[c.status] || "text-zinc-500"
                          }`}
                        >
                          {c.status || "—"}
                        </td>
                        <td className="px-3 py-2 text-[10px] text-zinc-500 font-mono truncate max-w-xs">
                          {c.endpoint || "—"}
                        </td>
                        <td className="px-3 py-2 text-[10px]">
                          <SecretSourceBadge
                            source={c.secret_source}
                            hasArn={!!c.secret_arn}
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </Section>
      )}

      {showConfirm && (
        <div className="fixed inset-0 z-50 bg-zinc-950/80 backdrop-blur flex items-center justify-center p-6">
          <div className="w-full max-w-lg border border-amber-500/40 bg-zinc-900 p-6 shadow-2xl">
            <div className="font-mono text-[10px] tracking-[0.18em] uppercase text-amber-400 mb-2">
              please review
            </div>
            <h2 className="text-xl font-semibold text-zinc-100 mb-3">
              Register {selectedClusters.length} cluster
              {selectedClusters.length === 1 ? "" : "s"}?
            </h2>
            <div className="text-sm text-zinc-400 space-y-2 mb-5 leading-relaxed">
              <p>
                DBOps는 등록된 클러스터에 대해{" "}
                <span className="text-zinc-200">read-only 인스펙션 쿼리</span>
                (pg_stat_*, information_schema 등)를 실행하고 메트릭을 캐시 DB에
                저장합니다.
              </p>
              <p>
                채팅·AI insight를 사용할 때마다{" "}
                <span className="text-zinc-200">Bedrock 토큰 비용</span>이
                발생합니다. Cost 탭에서 모니터링 가능합니다.
              </p>
              <p>언제든 클러스터 행에서 등록을 해제할 수 있습니다.</p>
            </div>
            <div className="border-t border-zinc-800 pt-3 max-h-40 overflow-y-auto text-xs font-mono text-zinc-400 space-y-1">
              {selectedClusters.map((c) => (
                <div key={c.cluster_id} className="flex items-center gap-2">
                  <span className="w-1.5 h-1.5 bg-amber-400 rounded-full" />
                  <span className="text-zinc-300">{c.cluster_id}</span>
                  <span className="text-zinc-600">·</span>
                  <span>{c.region}</span>
                  <span className="text-zinc-600">·</span>
                  <span>{c.engine}</span>
                </div>
              ))}
            </div>
            <div className="flex gap-2 mt-5">
              <button
                onClick={handleBulkRegister}
                disabled={registering}
                className="flex-1 text-sm font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors"
              >
                {registering
                  ? "등록 중…"
                  : `동의하고 ${selectedClusters.length}개 등록`}
              </button>
              <button
                onClick={() => setShowConfirm(false)}
                disabled={registering}
                className="text-sm px-4 py-2 border border-zinc-700 text-zinc-400 hover:text-zinc-200"
              >
                취소
              </button>
            </div>
          </div>
        </div>
      )}

      {showForm && (
        <Section eyebrow="신규 등록" title="Aurora 클러스터 등록">
          <div className="border border-zinc-800 bg-zinc-900/40 p-6 space-y-5">
            {/* Mode toggle — drives whether spoke_role_arn is shown.
                Same-account is the common case; cross-account opens the
                STS AssumeRole path with prominent guidance. */}
            <div>
              <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
                배포 방식
              </div>
              <div className="grid grid-cols-2 border border-zinc-800">
                {(
                  [
                    {
                      key: "same-account",
                      title: "이 계정의 Aurora",
                      hint: "DBOps와 같은 AWS 계정에서 실행 중인 클러스터",
                    },
                    {
                      key: "cross-account",
                      title: "다른 계정 (cross-account)",
                      hint: "STS AssumeRole로 접근할 spoke role 필요",
                    },
                  ] as const
                ).map((m) => {
                  const active = registerMode === m.key;
                  return (
                    <button
                      key={m.key}
                      type="button"
                      onClick={() => {
                        setRegisterMode(m.key);
                        // Clearing spoke_role_arn when switching back to
                        // same-account so it doesn't accidentally hitch
                        // along in a future submit.
                        if (m.key === "same-account") {
                          setForm((f) => ({ ...f, spoke_role_arn: "" }));
                        }
                      }}
                      className={`text-left px-4 py-3 transition-colors ${
                        active
                          ? "bg-zinc-950 border-l-2 border-amber-500"
                          : "bg-zinc-900/30 text-zinc-500 hover:bg-zinc-950/50 hover:text-zinc-300"
                      }`}
                    >
                      <div
                        className={`text-sm ${
                          active ? "text-zinc-100" : "text-zinc-400"
                        }`}
                      >
                        {m.title}
                      </div>
                      <div className="text-[11px] text-zinc-500 mt-0.5">
                        {m.hint}
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <Field
                label="Cluster ID"
                value={form.cluster_id}
                onChange={(v) => setForm({ ...form, cluster_id: v })}
                placeholder="my-aurora-cluster"
                mono
              />
              <Field
                label="Account ID"
                value={form.account_id}
                onChange={(v) => setForm({ ...form, account_id: v })}
                placeholder="123456789012"
                mono
              />
              <Field
                label="Region"
                value={form.region}
                onChange={(v) => setForm({ ...form, region: v })}
                placeholder="ap-northeast-2"
                mono
              />
              <div>
                <label className="block text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5">
                  Engine
                </label>
                <select
                  value={form.engine}
                  onChange={(e) => setForm({ ...form, engine: e.target.value })}
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 focus:outline-none focus:border-amber-500/60"
                >
                  <option value="aurora-postgresql">Aurora PostgreSQL</option>
                  <option value="aurora-mysql">Aurora MySQL</option>
                </select>
              </div>
              {registerMode === "cross-account" && (
                <Field
                  label="Spoke Role ARN"
                  value={form.spoke_role_arn}
                  onChange={(v) => setForm({ ...form, spoke_role_arn: v })}
                  placeholder="arn:aws:iam::<account>:role/dbops-spoke-role"
                  mono
                  fullWidth
                />
              )}
            </div>

            {registerMode === "same-account" ? (
              <div className="border-l-2 border-emerald-500/40 bg-emerald-500/5 px-3 py-2 text-[11px] text-zinc-400 leading-relaxed">
                같은 계정의 Aurora를 등록합니다. DBOps의 Lambda execution role이
                직접{" "}
                <span className="font-mono text-zinc-300">
                  rds:DescribeDBClusters
                </span>{" "}
                권한을 가지면 추가 설정 없이 연결됩니다.
              </div>
            ) : (
              <div className="border-l-2 border-amber-500/40 bg-amber-500/5 px-3 py-2 text-[11px] text-zinc-400 leading-relaxed space-y-1">
                <div>
                  Cross-account 등록 시 spoke 계정에 STS AssumeRole + RDS
                  describe 권한이 있는 role이 필요합니다. 등록 시점에 STS{" "}
                  <span className="font-mono text-zinc-300">AssumeRole</span> +{" "}
                  <span className="font-mono text-zinc-300">
                    rds:DescribeDBClusters
                  </span>{" "}
                  로 연결을 검증한 뒤 저장합니다.
                </div>
                <button
                  type="button"
                  onClick={() => setSetupGuideOpen(true)}
                  className="text-amber-300 hover:text-amber-200 underline underline-offset-2"
                >
                  Cross-account 설정 가이드 →
                </button>
              </div>
            )}

            <div className="flex gap-2 pt-1">
              <button
                disabled={submitting}
                onClick={handleRegister}
                className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:bg-zinc-700 disabled:text-zinc-500 transition-colors"
              >
                {submitting ? "검증 중…" : "등록 + 연결 검증"}
              </button>
              <button
                onClick={async () => {
                  if (!form.cluster_id || !form.region) {
                    setFeedback({
                      kind: "err",
                      msg: "cluster_id + region 이 필요합니다.",
                    });
                    return;
                  }
                  setTesting(true);
                  setTestResult(null);
                  setFeedback(null);
                  try {
                    const r = await testClusterConnection({
                      cluster_id: form.cluster_id,
                      region: form.region,
                      spoke_role_arn:
                        registerMode === "cross-account"
                          ? form.spoke_role_arn
                          : undefined,
                    });
                    setTestResult(r);
                  } catch (e) {
                    setFeedback({
                      kind: "err",
                      msg: e instanceof Error ? e.message : "test failed",
                    });
                  } finally {
                    setTesting(false);
                  }
                }}
                disabled={testing}
                className="text-xs px-4 py-2 border border-zinc-700 text-zinc-300 hover:border-amber-500/60 hover:text-amber-200 disabled:opacity-50 transition-colors"
                title="저장 없이 AssumeRole + DescribeDBClusters 만 실행해 보기"
              >
                {testing ? "테스트 중…" : "연결만 테스트"}
              </button>
              <button
                onClick={() => setShowForm(false)}
                className="text-xs px-4 py-2 border border-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
              >
                취소
              </button>
            </div>

            {testResult && (
              <div className="border border-zinc-800 bg-zinc-950 p-4 mt-3">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
                  Pre-flight 결과 ·{" "}
                  <span
                    className={
                      testResult.ok ? "text-emerald-400" : "text-rose-400"
                    }
                  >
                    {testResult.ok ? "PASS" : "FAIL"}
                  </span>
                </div>
                <div className="space-y-1.5">
                  {testResult.steps.map((s, i) => (
                    <div key={i} className="flex items-baseline gap-3 text-xs">
                      <span
                        className={`font-mono w-10 ${
                          s.status === "ok"
                            ? "text-emerald-400"
                            : s.status === "failed"
                              ? "text-rose-400"
                              : s.status === "warning"
                                ? "text-amber-400"
                                : "text-zinc-500"
                        }`}
                      >
                        {s.status === "ok"
                          ? "✓"
                          : s.status === "failed"
                            ? "✗"
                            : s.status === "warning"
                              ? "⚠"
                              : "—"}
                      </span>
                      <span className="font-mono text-zinc-300 w-40 flex-shrink-0">
                        {s.name}
                      </span>
                      <span className="text-zinc-400 flex-1 break-all">
                        {s.error ||
                          s.note ||
                          [s.engine, s.version, s.endpoint, s.secret_arn]
                            .filter(Boolean)
                            .join(" · ")}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </Section>
      )}

      <Section
        eyebrow="등록 현황"
        title={`등록된 클러스터 ${clusters.length}개`}
        description="이 페이지는 등록/검증/관리 전용입니다. 실시간 메트릭은 Fleet 또는 Dashboard에서."
      >
        {clustersError ? (
          <div className="bg-rose-500/10 border border-rose-500/30 rounded-lg p-4 text-sm">
            <div className="text-rose-300 font-medium mb-1">
              클러스터 목록을 불러오지 못했습니다
            </div>
            <div className="text-zinc-400">
              {clustersError} — 기존 등록 클러스터가 사라진 것이 아니라 조회
              실패 상태입니다.
            </div>
            <button
              onClick={loadClusters}
              className="mt-2 rounded border border-zinc-700 px-3 py-1 text-zinc-200 hover:bg-zinc-800"
            >
              다시 시도
            </button>
          </div>
        ) : clusters.length === 0 ? (
          <EmptyState
            eyebrow="클러스터 없음"
            title="첫 Aurora 클러스터를 등록해보세요"
            description="Cluster ID, account, region을 입력하면 RDS Data API 기반 메트릭 수집이 시작됩니다."
            primary={{
              onClick: () => setShowForm(true),
              label: "+ 클러스터 등록",
            }}
            secondary={{ href: "/chat", label: "먼저 에이전트에게 물어보기" }}
          />
        ) : (
          <>
            {/* Group clusters by engine family. Each non-empty family gets a
                small section header row, then its clusters rendered below. */}
            {(() => {
              const byFamily = groupByEngineFamily(clusters);
              const sections = FAMILY_ORDER.map((fam) => ({
                fam,
                meta: FAMILY_META[fam],
                items: byFamily[fam],
              })).filter((s) => s.items.length > 0);

              return sections.map(({ fam, meta, items }, sIdx) => (
                <div key={fam} className={sIdx > 0 ? "mt-6" : ""}>
                  {/* Family section header — matches the table's existing label
                      treatment: muted caps with a coloured accent dot. */}
                  <div className="flex items-center gap-2 mb-2">
                    <span
                      className={`w-2 h-2 rounded-full flex-shrink-0 ${meta.accent}`}
                    />
                    <span className="text-[11px] uppercase tracking-wider text-zinc-400 font-medium">
                      {meta.label}
                    </span>
                    <span className="text-[10px] text-zinc-600">
                      {items.length}
                    </span>
                  </div>

                  {/* Mobile card stack */}
                  <div className="md:hidden space-y-3">
                    {items.map((c) => {
                      const conn = c.connection_status || "untested";
                      const connStyle =
                        CONN_STYLES[conn] || CONN_STYLES.untested;
                      const statusColor =
                        STATUS_STYLES[c.status || ""] || "text-zinc-500";
                      return (
                        <div
                          key={c.cluster_id}
                          className="border border-zinc-800 rounded-lg p-3 bg-zinc-900/40"
                        >
                          <div className="flex items-start justify-between gap-2 mb-2">
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-2 flex-wrap">
                                <span className="text-zinc-100 font-mono text-xs truncate">
                                  {displayName(c)}
                                </span>
                                {c.is_demo && (
                                  <span className="px-1.5 py-0.5 text-[9px] font-mono uppercase bg-purple-500/15 text-purple-300 border border-purple-500/40">
                                    demo
                                  </span>
                                )}
                              </div>
                              <div className="text-[10px] text-zinc-500 font-mono mt-0.5">
                                {c.engine || "—"}
                                {c.engine_version ? ` ${c.engine_version}` : ""}
                              </div>
                            </div>
                            <span
                              className={`shrink-0 text-[10px] font-mono ${statusColor}`}
                            >
                              {c.status || "—"}
                            </span>
                          </div>
                          <div className="grid grid-cols-2 gap-1.5 text-[11px] mb-2">
                            <div className="text-zinc-400">
                              <span className="text-zinc-600">acct: </span>
                              <span className="font-mono text-zinc-300">
                                {c.account_id}
                              </span>
                            </div>
                            <div className="text-zinc-400">
                              <span className="text-zinc-600">region: </span>
                              <span className="font-mono text-zinc-300">
                                {c.region}
                              </span>
                            </div>
                            <div className="text-zinc-400">
                              <span className="text-zinc-600">conn: </span>
                              <span
                                className={`px-1 py-px border text-[9px] font-mono ${connStyle.classes}`}
                              >
                                {connStyle.label}
                              </span>
                            </div>
                            <div className="text-zinc-400">
                              <span className="text-zinc-600">added: </span>
                              <span className="text-zinc-300">
                                {relTime(c.registered_at)}
                              </span>
                            </div>
                          </div>
                          <div className="flex items-center justify-between pt-1.5 border-t border-zinc-800">
                            <Link
                              href={`/dashboard?cluster=${encodeURIComponent(
                                c.cluster_id,
                              )}`}
                              className="text-xs text-amber-400/90 hover:text-amber-300"
                            >
                              dashboard →
                            </Link>
                            {admin && (
                              <button
                                onClick={() => handleDelete(c)}
                                disabled={deletingId === c.cluster_id}
                                className="text-[11px] text-zinc-500 hover:text-rose-300 disabled:opacity-50"
                              >
                                {deletingId === c.cluster_id ? "…" : "delete"}
                              </button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {/* Desktop table */}
                  <div className="hidden md:block border border-zinc-800 overflow-hidden">
                    <table className="w-full text-sm">
                      <thead className="bg-zinc-900/60 text-[10px] uppercase tracking-wider text-zinc-500">
                        <tr>
                          <th className="text-left px-4 py-2.5 font-medium">
                            cluster
                          </th>
                          <th className="text-left px-4 py-2.5 font-medium">
                            engine
                          </th>
                          <th className="text-left px-4 py-2.5 font-medium">
                            account · region
                          </th>
                          <th className="text-left px-4 py-2.5 font-medium">
                            status
                          </th>
                          <th className="text-left px-4 py-2.5 font-medium">
                            connection
                          </th>
                          <th className="text-left px-4 py-2.5 font-medium">
                            ETL
                          </th>
                          <th className="text-left px-4 py-2.5 font-medium">
                            registered
                          </th>
                          <th className="text-right px-4 py-2.5 font-medium">
                            actions
                          </th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-zinc-800">
                        {items.map((c) => {
                          const conn = c.connection_status || "untested";
                          const connStyle =
                            CONN_STYLES[conn] || CONN_STYLES.untested;
                          const statusColor =
                            STATUS_STYLES[c.status || ""] || "text-zinc-500";
                          return (
                            <tr
                              key={c.cluster_id}
                              className="hover:bg-zinc-900/40"
                            >
                              <td className="px-4 py-2.5">
                                <div className="flex items-center gap-2">
                                  <div className="text-zinc-100 font-mono text-xs">
                                    {displayName(c)}
                                  </div>
                                  {c.is_demo && (
                                    <span className="px-1.5 py-0.5 text-[9px] font-mono tracking-wider uppercase bg-purple-500/15 text-purple-300 border border-purple-500/40">
                                      demo
                                    </span>
                                  )}
                                </div>
                                {c.spoke_role_arn && (
                                  <div
                                    className="text-[10px] text-zinc-500 mt-0.5 font-mono truncate max-w-xs"
                                    title={c.spoke_role_arn}
                                  >
                                    ⇢ {c.spoke_role_arn}
                                  </div>
                                )}
                              </td>
                              <td className="px-4 py-2.5">
                                <div className="text-zinc-300 text-xs">
                                  {c.engine || "—"}
                                </div>
                                {c.engine_version && (
                                  <div className="text-[10px] text-zinc-500 font-mono">
                                    {c.engine_version}
                                  </div>
                                )}
                              </td>
                              <td className="px-4 py-2.5 text-zinc-300 text-xs font-mono">
                                {c.account_id}
                                <div className="text-zinc-500 text-[10px]">
                                  {c.region}
                                </div>
                              </td>
                              <td
                                className={`px-4 py-2.5 text-xs ${statusColor}`}
                              >
                                {c.status || "—"}
                              </td>
                              <td className="px-4 py-2.5">
                                <span
                                  className={`px-1.5 py-0.5 border text-[10px] font-mono ${connStyle.classes}`}
                                  title={c.connection_error || ""}
                                >
                                  {connStyle.label}
                                </span>
                              </td>
                              <td className="px-4 py-2.5">
                                <EtlBadge
                                  status={c.etl_status}
                                  latestTs={c.etl_latest_ts}
                                  rows={c.etl_rows_24h}
                                />
                              </td>
                              <td className="px-4 py-2.5 text-zinc-500 text-xs">
                                {relTime(c.registered_at)}
                              </td>
                              <td className="px-4 py-2.5 text-right">
                                <div className="flex items-center justify-end gap-3">
                                  <Link
                                    href={`/dashboard?cluster=${encodeURIComponent(
                                      c.cluster_id,
                                    )}`}
                                    className="text-xs text-amber-400/90 hover:text-amber-300"
                                  >
                                    dashboard →
                                  </Link>
                                  {admin && (
                                    <button
                                      onClick={() => handleDelete(c)}
                                      disabled={deletingId === c.cluster_id}
                                      className="text-[11px] text-zinc-500 hover:text-rose-300 disabled:opacity-50 transition-colors"
                                      title={
                                        c.is_demo
                                          ? "데모 클러스터 및 합성 데이터 삭제"
                                          : "레지스트리에서 해제"
                                      }
                                    >
                                      {deletingId === c.cluster_id
                                        ? "…"
                                        : "delete"}
                                    </button>
                                  )}
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              ));
            })()}
          </>
        )}
      </Section>

      <SetupGuideModal
        open={setupGuideOpen}
        onClose={() => setSetupGuideOpen(false)}
        clusterId={form.cluster_id || undefined}
        region={form.region || undefined}
      />
    </PageBody>
  );
}

function SecretSourceBadge({
  source,
  hasArn,
}: {
  source?: "convention" | "master_fallback" | "missing";
  hasArn: boolean;
}) {
  // Server may not have emitted secret_source for cached or older payloads —
  // fall back to the legacy "managed / manual" rendering so the column is never blank.
  if (!source) {
    return (
      <span className="text-zinc-500 font-mono text-[10px]">
        {hasArn ? "✓ managed" : "— manual"}
      </span>
    );
  }
  if (source === "convention") {
    return (
      <span
        className="px-1.5 py-0.5 border text-[10px] font-mono bg-emerald-500/10 text-emerald-300 border-emerald-500/40"
        title="dbops/<cluster_id>/readonly 컨벤션 시크릿 자동 연결 (권장)"
      >
        ✓ convention
      </span>
    );
  }
  if (source === "master_fallback") {
    return (
      <span
        className="px-1.5 py-0.5 border text-[10px] font-mono bg-amber-500/10 text-amber-300 border-amber-500/40"
        title="컨벤션 시크릿이 없어 master 시크릿으로 폴백. 프로덕션 사용 전 전용 계정 등록 권장."
      >
        ⚠ master fallback
      </span>
    );
  }
  return (
    <span
      className="px-1.5 py-0.5 border text-[10px] font-mono bg-rose-500/10 text-rose-300 border-rose-500/40"
      title="사용 가능한 시크릿이 없습니다. 설정 가이드를 참고하세요."
    >
      ✗ missing
    </span>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  mono,
  fullWidth,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
  fullWidth?: boolean;
}) {
  return (
    <div className={fullWidth ? "md:col-span-2" : ""}>
      <label className="block text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5">
        {label}
      </label>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={`w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 focus:outline-none focus:border-amber-500/60 transition-colors ${
          mono ? "font-mono" : ""
        }`}
      />
    </div>
  );
}
