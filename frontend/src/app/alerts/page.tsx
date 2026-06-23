"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import {
  fetchAlertRules,
  fetchClusters,
  createAlertRule,
  updateAlertRule,
  deleteAlertRule,
  fetchAlertSubscriptions,
  createAlertSubscription,
  deleteAlertSubscription,
  fetchAlertImpact,
  type AlertImpact,
  apiUrl,
} from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";
import { isAdmin } from "@/lib/auth";
import { getSelectedCluster } from "@/lib/selected-cluster";
import { SearchableClusterSelect } from "@/components/design-system/searchable-cluster-select";

interface Rule {
  id: number;
  cluster_id: string;
  name: string;
  metric_type: string;
  comparison: string;
  threshold: number | string;
  enabled: boolean;
  last_triggered_at: string | null;
  created_at: string;
  // Backend-computed health of the metric stream feeding this rule.
  // Older API payloads (cached) may omit these — guard for undefined.
  latest_metric_ts?: string | null;
  data_status?: "fresh" | "stale" | "no_data";
  // Compound rules carry their JSON-encoded conditions DSL. Older payloads
  // (and legacy single-threshold rules) omit it.
  conditions_json?: string | null;
  // Slack-ack state — written by the /api/slack/interactive endpoint.
  // The badge only renders when last_acked_at is after last_triggered_at;
  // older acks are considered stale once the rule fires again.
  last_acked_at?: string | null;
  last_acked_by?: string | null;
}

interface CompoundOperand {
  metric_type: string;
  comparison: string;
  threshold: number;
  window_minutes?: number;
  agg?: string;
}

interface CompoundConditions {
  logic: "and" | "or";
  operands: CompoundOperand[];
}

function parseConditions(
  raw: string | null | undefined,
): CompoundConditions | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as CompoundConditions;
    if (!parsed || !Array.isArray(parsed.operands)) return null;
    return parsed;
  } catch {
    return null;
  }
}

function DataStatusBadge({
  status,
  latestTs,
}: {
  status?: Rule["data_status"];
  latestTs?: string | null;
}) {
  // Fallback for API responses that pre-date the data_status field.
  if (!status) {
    return <span className="text-zinc-600 text-[10px] font-mono">—</span>;
  }
  if (status === "fresh") {
    return (
      <span
        className="px-1.5 py-0.5 border text-[10px] font-mono bg-emerald-500/10 text-emerald-300 border-emerald-500/40"
        title={
          latestTs
            ? `last metric: ${new Date(latestTs).toLocaleString()}`
            : "metric stream within 10 minutes"
        }
      >
        fresh
      </span>
    );
  }
  if (status === "stale") {
    return (
      <span
        className="px-1.5 py-0.5 border text-[10px] font-mono bg-amber-500/10 text-amber-300 border-amber-500/40"
        title={
          latestTs
            ? `last metric: ${new Date(
                latestTs,
              ).toLocaleString()} — evaluator skips this rule until newer data arrives`
            : "metric stream stale — evaluator will skip this rule"
        }
      >
        stale
      </span>
    );
  }
  return (
    <span
      className="px-1.5 py-0.5 border text-[10px] font-mono bg-rose-500/10 text-rose-300 border-rose-500/40"
      title="이 cluster + metric 조합으로 수집된 metric_snapshot이 없습니다 — 클러스터 등록 상태와 ETL 파이프라인을 확인하세요"
    >
      no data
    </span>
  );
}

const METRIC_OPTIONS = [
  "cpu",
  "aas",
  "db_connections",
  "conn_active",
  "deadlocks",
  "read_iops",
  "write_iops",
  "replica_lag_ms",
  "storage_bytes",
  "mem_free",
];

const COMP_OPS = [">", ">=", "<", "<=", "==", "!="] as const;

// Curated rule presets — one click to populate the builder with a
// DBA-canonical condition. Keeps the builder accessible to operators
// who don't have every metric_type memorized. Each template is shown
// as a clickable chip above the operand grid.
type CompoundOp = {
  metric_type: string;
  comparison: (typeof COMP_OPS)[number];
  threshold: number;
  window_minutes: number;
  agg: "max" | "min" | "avg" | "last";
};
const RULE_TEMPLATES: {
  label: string;
  hint: string;
  logic: "and" | "or";
  operands: CompoundOp[];
}[] = [
  {
    label: "CPU 지속 스파이크",
    hint: "CPU avg > 80% over 10min",
    logic: "and",
    operands: [
      {
        metric_type: "cpu",
        comparison: ">",
        threshold: 80,
        window_minutes: 10,
        agg: "avg",
      },
    ],
  },
  {
    label: "Connection 폭주",
    hint: "active connections last > 90",
    logic: "and",
    operands: [
      {
        metric_type: "conn_active",
        comparison: ">",
        threshold: 90,
        window_minutes: 5,
        agg: "last",
      },
    ],
  },
  {
    label: "AAS 과부하",
    hint: "AAS avg > 5 for 10min",
    logic: "and",
    operands: [
      {
        metric_type: "aas",
        comparison: ">",
        threshold: 5,
        window_minutes: 10,
        agg: "avg",
      },
    ],
  },
  {
    label: "Replica lag",
    hint: "replica_lag_ms last > 30s",
    logic: "and",
    operands: [
      {
        metric_type: "replica_lag_ms",
        comparison: ">",
        threshold: 30000,
        window_minutes: 5,
        agg: "last",
      },
    ],
  },
  {
    label: "Deadlock 발생",
    hint: "deadlocks max > 5 in 5min",
    logic: "and",
    operands: [
      {
        metric_type: "deadlocks",
        comparison: ">",
        threshold: 5,
        window_minutes: 5,
        agg: "max",
      },
    ],
  },
  {
    label: "Write storm + CPU",
    hint: "write_iops max > 50k AND cpu avg > 70%",
    logic: "and",
    operands: [
      {
        metric_type: "write_iops",
        comparison: ">",
        threshold: 50000,
        window_minutes: 5,
        agg: "max",
      },
      {
        metric_type: "cpu",
        comparison: ">",
        threshold: 70,
        window_minutes: 5,
        agg: "avg",
      },
    ],
  },
];

export default function AlertsPage() {
  const [rules, setRules] = useState<Rule[]>([]);
  // Impact panel: which rule's "what was going on?" context is expanded
  // right now, plus its fetched data. Keyed by rule id so toggling the
  // same row collapses it.
  const [impactOpenId, setImpactOpenId] = useState<number | null>(null);
  const [impactData, setImpactData] = useState<AlertImpact | null>(null);
  const [impactLoading, setImpactLoading] = useState(false);
  const [impactError, setImpactError] = useState<string | null>(null);

  const openImpact = useCallback(
    async (id: number) => {
      if (impactOpenId === id) {
        setImpactOpenId(null);
        setImpactData(null);
        return;
      }
      setImpactOpenId(id);
      setImpactData(null);
      setImpactError(null);
      setImpactLoading(true);
      try {
        const data = await fetchAlertImpact(id);
        setImpactData(data);
      } catch (e) {
        setImpactError(e instanceof Error ? e.message : String(e));
      } finally {
        setImpactLoading(false);
      }
    },
    [impactOpenId],
  );
  const [clusters, setClusters] = useState<{ cluster_id: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [admin, setAdmin] = useState(false);
  useEffect(() => {
    setAdmin(isAdmin());
  }, []);

  const [newRule, setNewRule] = useState({
    cluster_id: "",
    metric_type: "cpu",
    comparison: ">" as (typeof COMP_OPS)[number],
    threshold: 80,
    name: "",
  });

  // Compound mode: build an AND/OR rule from N operands. State kept separate
  // from the simple form so toggling back and forth doesn't clobber either.
  const [compoundMode, setCompoundMode] = useState(false);
  const [compound, setCompound] = useState<{
    logic: "and" | "or";
    operands: Array<{
      metric_type: string;
      comparison: (typeof COMP_OPS)[number];
      threshold: number;
      window_minutes: number;
      agg: "max" | "min" | "avg" | "last";
    }>;
  }>({
    logic: "and",
    operands: [
      {
        metric_type: "cpu",
        comparison: ">",
        threshold: 80,
        window_minutes: 10,
        agg: "max",
      },
      {
        metric_type: "db_connections",
        comparison: ">",
        threshold: 100,
        window_minutes: 10,
        agg: "max",
      },
    ],
  });

  const [subs, setSubs] = useState<
    { subscription_arn: string; protocol: string; endpoint: string }[]
  >([]);
  const [topicArn, setTopicArn] = useState<string>("");
  const [newSub, setNewSub] = useState({ protocol: "email", endpoint: "" });

  const reloadSubs = () =>
    fetchAlertSubscriptions()
      .then((d) => {
        setSubs(d.subscriptions || []);
        setTopicArn(d.topic_arn || "");
      })
      .catch((e) => setErr(`Subscriptions: ${e.message}`));

  const reload = () =>
    fetchAlertRules()
      .then((d) => setRules(d.rules || []))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));

  useEffect(() => {
    fetchClusters()
      .then((cs) => {
        setClusters(cs);
        if (cs.length > 0) {
          // Prefer the globally selected cluster (⌘K / header / other pages) so
          // a new rule defaults to the cluster the DBA is focused on; fall back
          // to the first cluster when the selection isn't a real cluster.
          const sel = getSelectedCluster();
          const pick =
            sel && cs.some((c: { cluster_id: string }) => c.cluster_id === sel)
              ? sel
              : cs[0].cluster_id;
          setNewRule((r) => ({ ...r, cluster_id: pick }));
        }
      })
      .catch((e) => setErr(`Clusters: ${e.message}`));
    reload();
    reloadSubs();
  }, []);

  const submitSub = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newSub.endpoint || submitting) return;
    setSubmitting(true);
    try {
      await createAlertSubscription(newSub.protocol, newSub.endpoint);
      setNewSub({ protocol: "email", endpoint: "" });
      reloadSubs();
    } catch (err) {
      setErr(err instanceof Error ? err.message : "Subscription failed");
    } finally {
      setSubmitting(false);
    }
  };

  const removeSub = async (arn: string) => {
    await deleteAlertSubscription(arn);
    reloadSubs();
  };

  // Double-submit guard: without it a double-click on 규칙 추가 created the
  // same rule twice (no idempotency on the API side either).
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newRule.cluster_id || submitting) return;
    setSubmitting(true);
    try {
      await createAlertRule(newRule);
      setNewRule((r) => ({ ...r, name: "" }));
      reload();
    } catch (err) {
      setErr(err instanceof Error ? err.message : "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  const submitCompound = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newRule.cluster_id || compound.operands.length === 0 || submitting)
      return;
    setSubmitting(true);
    try {
      await createAlertRule({
        cluster_id: newRule.cluster_id,
        name: newRule.name || undefined,
        conditions: compound,
      });
      setNewRule((r) => ({ ...r, name: "" }));
      reload();
    } catch (err) {
      setErr(err instanceof Error ? err.message : "Failed");
    } finally {
      setSubmitting(false);
    }
  };

  const updateOperand = (
    i: number,
    patch: Partial<(typeof compound.operands)[number]>,
  ) =>
    setCompound((c) => ({
      ...c,
      operands: c.operands.map((o, idx) =>
        idx === i ? { ...o, ...patch } : o,
      ),
    }));

  const addOperand = () =>
    setCompound((c) => ({
      ...c,
      operands: [
        ...c.operands,
        {
          metric_type: "cpu",
          comparison: ">" as (typeof COMP_OPS)[number],
          threshold: 80,
          window_minutes: 10,
          agg: "max" as const,
        },
      ],
    }));

  const removeOperand = (i: number) =>
    setCompound((c) => ({
      ...c,
      operands: c.operands.filter((_, idx) => idx !== i),
    }));

  const toggle = async (id: number, enabled: boolean) => {
    await updateAlertRule(id, { enabled: !enabled });
    reload();
  };

  const remove = async (id: number) => {
    await deleteAlertRule(id);
    reload();
  };

  return (
    <PageBody>
      <PageHeader
        eyebrow="설정"
        title="알림 규칙"
        description={`총 ${rules.length}개 · 5분마다 metric_snapshots를 평가해서 조건 충족 시 발화합니다.`}
      />

      {err && (
        <div className="mb-6 px-4 py-3 border bg-rose-500/10 border-rose-500/30 text-rose-300 text-sm">
          {err}
        </div>
      )}

      {!admin && (
        <div className="mb-6 px-3 py-2 border border-zinc-800 text-[11px] uppercase tracking-wider text-zinc-500">
          읽기 전용 · viewer 권한 — 쓰기 액션은 숨겨집니다
        </div>
      )}

      {admin && (
        <Section
          eyebrow="새 규칙"
          title="알림 임계값 정의"
          actions={
            <div className="flex border border-zinc-700 font-mono">
              <button
                type="button"
                onClick={() => setCompoundMode(false)}
                className={`text-[10px] px-3 py-1 uppercase tracking-wider transition-colors ${
                  !compoundMode
                    ? "bg-amber-500 text-zinc-950"
                    : "text-zinc-400 hover:text-zinc-200"
                }`}
              >
                단순
              </button>
              <button
                type="button"
                onClick={() => setCompoundMode(true)}
                className={`text-[10px] px-3 py-1 uppercase tracking-wider transition-colors ${
                  compoundMode
                    ? "bg-amber-500 text-zinc-950"
                    : "text-zinc-400 hover:text-zinc-200"
                }`}
              >
                복합 (AND/OR)
              </button>
            </div>
          }
        >
          {!compoundMode && (
            <form
              onSubmit={submit}
              className="border border-zinc-800 bg-zinc-900/40 p-6 grid grid-cols-1 md:grid-cols-6 gap-3 items-end"
            >
              <div className="md:col-span-2">
                <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                  Cluster
                </label>
                <div className="mt-1">
                  <SearchableClusterSelect
                    value={newRule.cluster_id}
                    onChange={(id) =>
                      setNewRule({ ...newRule, cluster_id: id })
                    }
                    clusters={clusters}
                  />
                </div>
              </div>
              <div>
                <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                  Metric
                </label>
                <select
                  value={newRule.metric_type}
                  onChange={(e) =>
                    setNewRule({ ...newRule, metric_type: e.target.value })
                  }
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60 transition-colors"
                >
                  {METRIC_OPTIONS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                  Op
                </label>
                <select
                  value={newRule.comparison}
                  onChange={(e) =>
                    setNewRule({
                      ...newRule,
                      comparison: e.target.value as (typeof COMP_OPS)[number],
                    })
                  }
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60 transition-colors"
                >
                  {COMP_OPS.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                  Threshold
                </label>
                <input
                  type="number"
                  step="0.01"
                  value={newRule.threshold}
                  onChange={(e) =>
                    setNewRule({
                      ...newRule,
                      threshold: Number(e.target.value),
                    })
                  }
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60 transition-colors"
                />
              </div>
              <button
                type="submit"
                disabled={submitting}
                className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors disabled:opacity-50"
              >
                규칙 추가
              </button>
            </form>
          )}

          {compoundMode && (
            <form
              onSubmit={submitCompound}
              className="border border-zinc-800 bg-zinc-900/40 p-6 space-y-4"
            >
              {/* Rule templates — one click populates the builder with a
                  DBA-canonical condition. Helps operators who don't have
                  every metric_type memorized. */}
              <div>
                <div className="text-[10px] text-zinc-500 uppercase tracking-wider mb-2">
                  Template (선택)
                </div>
                <div className="flex flex-wrap gap-2">
                  {RULE_TEMPLATES.map((t) => (
                    <button
                      key={t.label}
                      type="button"
                      onClick={() =>
                        setCompound({
                          logic: t.logic,
                          operands: t.operands.map((o) => ({ ...o })),
                        })
                      }
                      title={t.hint}
                      className="text-[11px] px-3 py-1.5 border border-zinc-800 bg-zinc-950/60 text-zinc-300 hover:border-amber-500/60 hover:text-amber-200 transition-colors"
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3 items-end">
                <div>
                  <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                    Cluster
                  </label>
                  <select
                    value={newRule.cluster_id}
                    onChange={(e) =>
                      setNewRule({ ...newRule, cluster_id: e.target.value })
                    }
                    className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60 transition-colors"
                  >
                    {clusters.map((c) => (
                      <option key={c.cluster_id} value={c.cluster_id}>
                        {c.cluster_id}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                    결합 logic
                  </label>
                  <div className="flex border border-zinc-800 mt-1 font-mono">
                    {(["and", "or"] as const).map((l) => (
                      <button
                        key={l}
                        type="button"
                        onClick={() => setCompound({ ...compound, logic: l })}
                        className={`text-xs px-4 py-2 uppercase tracking-wider transition-colors ${
                          compound.logic === l
                            ? "bg-zinc-100 text-zinc-950"
                            : "text-zinc-400 hover:text-zinc-200 bg-zinc-950"
                        }`}
                      >
                        {l}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <div className="space-y-2">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                  Operands · 모두{" "}
                  <span className="text-amber-300 font-mono">
                    {compound.logic.toUpperCase()}
                  </span>{" "}
                  로 결합
                </div>
                {compound.operands.map((op, i) => (
                  <div
                    key={i}
                    className="border border-zinc-800 bg-zinc-950/60 p-3 grid grid-cols-1 md:grid-cols-[2fr_1fr_1fr_1fr_1fr_auto] gap-2 items-end"
                  >
                    <div>
                      <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                        Metric
                      </label>
                      <select
                        value={op.metric_type}
                        onChange={(e) =>
                          updateOperand(i, { metric_type: e.target.value })
                        }
                        className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 mt-1 focus:outline-none focus:border-amber-500/60"
                      >
                        {METRIC_OPTIONS.map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                        Agg
                      </label>
                      <select
                        value={op.agg}
                        onChange={(e) =>
                          updateOperand(i, {
                            agg: e.target.value as typeof op.agg,
                          })
                        }
                        className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 mt-1 focus:outline-none focus:border-amber-500/60"
                      >
                        {(["max", "min", "avg", "last"] as const).map((a) => (
                          <option key={a} value={a}>
                            {a}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                        Op
                      </label>
                      <select
                        value={op.comparison}
                        onChange={(e) =>
                          updateOperand(i, {
                            comparison: e.target
                              .value as (typeof COMP_OPS)[number],
                          })
                        }
                        className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 mt-1 focus:outline-none focus:border-amber-500/60"
                      >
                        {COMP_OPS.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                        Threshold
                      </label>
                      <input
                        type="number"
                        step="0.01"
                        value={op.threshold}
                        onChange={(e) =>
                          updateOperand(i, {
                            threshold: Number(e.target.value),
                          })
                        }
                        className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 mt-1 font-mono focus:outline-none focus:border-amber-500/60"
                      />
                    </div>
                    <div>
                      <label
                        className="text-[10px] text-zinc-500 uppercase tracking-wider"
                        title="평가 윈도우 (분) — 이 시간 내 데이터로 agg 계산"
                      >
                        Window (m)
                      </label>
                      <input
                        type="number"
                        min="1"
                        max="1440"
                        value={op.window_minutes}
                        onChange={(e) =>
                          updateOperand(i, {
                            window_minutes: Number(e.target.value),
                          })
                        }
                        className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 mt-1 font-mono focus:outline-none focus:border-amber-500/60"
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => removeOperand(i)}
                      disabled={compound.operands.length === 1}
                      className="text-[10px] uppercase tracking-wider px-2 py-1.5 text-zinc-500 hover:text-rose-300 disabled:opacity-30"
                      title="이 조건 삭제"
                    >
                      ✕
                    </button>
                  </div>
                ))}

                <button
                  type="button"
                  onClick={addOperand}
                  disabled={compound.operands.length >= 8}
                  className="text-[10px] uppercase tracking-wider px-3 py-1 border border-zinc-700 text-zinc-300 hover:border-amber-500 hover:text-amber-300 transition-colors disabled:opacity-30"
                >
                  + 조건 추가 ({compound.operands.length}/8)
                </button>
              </div>

              <div className="flex items-end justify-between gap-3 pt-2 border-t border-zinc-800">
                <div className="flex-1">
                  <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                    규칙 이름 (선택)
                  </label>
                  <input
                    type="text"
                    value={newRule.name}
                    onChange={(e) =>
                      setNewRule({ ...newRule, name: e.target.value })
                    }
                    placeholder="자동 생성됨 — 첫 operand + AND/OR + N"
                    className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60"
                  />
                </div>
                <button
                  type="submit"
                  disabled={submitting}
                  className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors disabled:opacity-50"
                >
                  복합 규칙 추가
                </button>
              </div>

              <div className="text-[11px] text-zinc-500 font-mono border-l-2 border-zinc-700 pl-2">
                평가 시점:{" "}
                {compound.operands
                  .map(
                    (o) =>
                      `${o.metric_type}(${o.agg},${o.window_minutes}m) ${o.comparison} ${o.threshold}`,
                  )
                  .join(` ${compound.logic.toUpperCase()} `)}
              </div>
            </form>
          )}
        </Section>
      )}

      <Section
        eyebrow="알림 채널"
        title="구독자"
        description={
          topicArn
            ? `SNS 토픽 ${topicArn}을 통해 fan-out`
            : "SNS 토픽이 설정되어 있지 않음"
        }
      >
        <div className="border border-zinc-800 bg-zinc-900/40 p-6">
          {admin && (
            <form
              onSubmit={submitSub}
              className="grid grid-cols-1 md:grid-cols-6 gap-3 items-end mb-3"
            >
              <div>
                <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                  Protocol
                </label>
                <select
                  value={newSub.protocol}
                  onChange={(e) =>
                    setNewSub({ ...newSub, protocol: e.target.value })
                  }
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60 transition-colors"
                >
                  <option value="email">Email</option>
                  <option value="sms">SMS</option>
                  <option value="https">HTTPS webhook</option>
                  <option value="slack-webhook">Slack incoming webhook</option>
                  <option value="teams-webhook">Microsoft Teams</option>
                  <option value="pagerduty-events-v2">
                    PagerDuty events-v2
                  </option>
                </select>
              </div>
              <div className="md:col-span-4">
                <label className="text-[10px] text-zinc-500 uppercase tracking-wider">
                  Endpoint
                </label>
                <input
                  value={newSub.endpoint}
                  onChange={(e) =>
                    setNewSub({ ...newSub, endpoint: e.target.value })
                  }
                  placeholder={
                    newSub.protocol === "email"
                      ? "dba@example.com"
                      : newSub.protocol === "sms"
                        ? "+821012345678"
                        : newSub.protocol === "slack-webhook"
                          ? "https://hooks.slack.com/services/T.../B.../..."
                          : newSub.protocol === "teams-webhook"
                            ? "https://<조직>.webhook.office.com/webhookb2/..."
                            : newSub.protocol === "pagerduty-events-v2"
                              ? "PagerDuty integration key (32 hex chars)"
                              : "https://example.com/webhook"
                  }
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60 transition-colors"
                />
                {newSub.protocol === "teams-webhook" && (
                  <p className="mt-1 text-[11px] text-zinc-500">
                    Incoming Webhook URL 또는 Workflows URL 모두 사용
                    가능합니다.
                  </p>
                )}
              </div>
              <button
                type="submit"
                disabled={submitting}
                className="text-xs font-medium px-4 py-2 bg-emerald-500 text-zinc-950 hover:bg-emerald-400 transition-colors disabled:opacity-50"
              >
                구독 추가
              </button>
            </form>
          )}
          {subs.length > 0 ? (
            // Mobile fallback: tables are too column-rich to card-ify
            // cleanly; horizontal scroll preserves info while letting
            // the page fit. Same pattern below for the rules table.
            <div className="overflow-x-auto">
              <table className="w-full text-sm border border-zinc-800 min-w-[640px]">
                <thead className="bg-zinc-900/60 border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-500">
                  <tr>
                    <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                      Protocol
                    </th>
                    <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                      Endpoint
                    </th>
                    <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                      Status
                    </th>
                    <th className="text-right px-3 py-2 text-zinc-400 font-medium">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800">
                  {subs.map((s, i) => {
                    const pending =
                      !s.subscription_arn ||
                      s.subscription_arn === "PendingConfirmation" ||
                      s.subscription_arn === "Deleted";
                    return (
                      <tr
                        key={`${s.subscription_arn}-${i}`}
                        className="hover:bg-zinc-900/40"
                      >
                        <td className="px-3 py-2 text-zinc-300 font-mono text-xs">
                          {s.protocol}
                        </td>
                        <td className="px-3 py-2 text-zinc-200 font-mono text-xs">
                          {s.endpoint}
                        </td>
                        <td className="px-3 py-2">
                          <span
                            className={`px-1.5 py-0.5 border text-[10px] font-mono ${
                              pending
                                ? "bg-amber-500/10 text-amber-300 border-amber-500/40"
                                : "bg-emerald-500/10 text-emerald-300 border-emerald-500/40"
                            }`}
                          >
                            {pending ? "승인 대기" : "활성"}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right">
                          {!pending && admin && (
                            <button
                              onClick={() => removeSub(s.subscription_arn)}
                              className="text-rose-400 hover:text-rose-300 text-xs"
                            >
                              구독 해지
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-zinc-500 text-xs py-2">
              아직 구독자가 없습니다. 이메일/SMS/Slack webhook을 추가하면 알림
              발생 시 전달됩니다.
            </div>
          )}
        </div>
      </Section>

      <SlackAckSetupGuide />

      <Section
        eyebrow="규칙"
        title={`등록된 알림 규칙 ${rules.length}개`}
        description="evaluator는 5분마다 실행되며, metric 데이터가 stale이거나 없는 규칙은 건너뜁니다."
      >
        {loading ? (
          <div className="text-zinc-500 text-sm">불러오는 중...</div>
        ) : rules.length === 0 ? (
          <EmptyState
            eyebrow="규칙 없음"
            title="첫 알림 규칙을 등록해보세요"
            description="위 폼에서 cluster + metric + threshold를 고르면 됩니다. evaluator가 5분마다 실행되고 SNS / Slack / PagerDuty 구독자에게 fan-out 됩니다."
          />
        ) : (
          <div className="border border-zinc-800 overflow-x-auto">
            <table className="w-full text-sm min-w-[768px]">
              <thead className="bg-zinc-900/60 border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-500">
                <tr>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    상태
                  </th>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    데이터
                  </th>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    클러스터
                  </th>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    규칙
                  </th>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    마지막 발생
                  </th>
                  <th className="text-right px-3 py-2 text-zinc-400 font-medium">
                    작업
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800">
                {rules.map((r) => (
                  <Fragment key={r.id}>
                    <tr className="hover:bg-zinc-900/40">
                      <td className="px-3 py-2">
                        <button
                          onClick={() => toggle(r.id, r.enabled)}
                          disabled={!admin}
                          className={`px-1.5 py-0.5 border text-[10px] font-mono transition-colors ${
                            r.enabled
                              ? "bg-emerald-500/10 text-emerald-300 border-emerald-500/40 hover:bg-emerald-500/20"
                              : "bg-zinc-700/40 text-zinc-400 border-zinc-700 hover:bg-zinc-700/60"
                          } ${admin ? "" : "cursor-not-allowed opacity-70"}`}
                        >
                          {r.enabled ? "활성" : "중지"}
                        </button>
                      </td>
                      <td className="px-3 py-2">
                        <DataStatusBadge
                          status={r.data_status}
                          latestTs={r.latest_metric_ts}
                        />
                      </td>
                      <td className="px-3 py-2 text-zinc-300 font-mono text-xs">
                        {r.cluster_id}
                      </td>
                      <td className="px-3 py-2 text-zinc-200 font-mono text-xs">
                        {(() => {
                          const comp = parseConditions(r.conditions_json);
                          if (!comp) {
                            return (
                              <>
                                {r.metric_type}{" "}
                                <span className="text-amber-400">
                                  {r.comparison}
                                </span>{" "}
                                {r.threshold}
                              </>
                            );
                          }
                          const join = ` ${comp.logic.toUpperCase()} `;
                          return (
                            <span
                              title={comp.operands
                                .map(
                                  (o) =>
                                    `${o.metric_type}(${o.agg ?? "max"},${
                                      o.window_minutes ?? 10
                                    }m) ${o.comparison} ${o.threshold}`,
                                )
                                .join(join)}
                            >
                              <span className="px-1 py-0.5 mr-1.5 bg-amber-500/15 text-amber-300 border border-amber-500/40 text-[10px] uppercase tracking-wider">
                                {comp.logic} · {comp.operands.length}
                              </span>
                              <span className="text-zinc-400">
                                {comp.operands
                                  .slice(0, 2)
                                  .map(
                                    (o) =>
                                      `${o.metric_type} ${o.comparison} ${o.threshold}`,
                                  )
                                  .join(join)}
                                {comp.operands.length > 2 && " …"}
                              </span>
                            </span>
                          );
                        })()}
                      </td>
                      <td className="px-3 py-2 text-zinc-400 text-xs">
                        {r.last_triggered_at
                          ? new Date(r.last_triggered_at).toLocaleString()
                          : "발화 이력 없음"}
                        {(() => {
                          // Ack badge: only when ack is newer than the latest
                          // trigger. If the rule has fired again since the ack
                          // we treat the ack as stale.
                          if (!r.last_acked_at) return null;
                          if (
                            r.last_triggered_at &&
                            new Date(r.last_acked_at) <=
                              new Date(r.last_triggered_at)
                          )
                            return null;
                          return (
                            <div
                              className="mt-1 inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 border border-emerald-500/40 bg-emerald-500/10 text-emerald-300 font-mono"
                              title={`acked ${new Date(
                                r.last_acked_at,
                              ).toLocaleString()}`}
                            >
                              <span>✓ acked</span>
                              {r.last_acked_by && (
                                <span className="text-zinc-400">
                                  @{r.last_acked_by}
                                </span>
                              )}
                            </div>
                          );
                        })()}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <div className="flex items-center justify-end gap-3">
                          {r.last_triggered_at && (
                            <button
                              onClick={() => openImpact(r.id)}
                              className="text-amber-300 hover:text-amber-200 text-xs underline underline-offset-2"
                              title="이 룰이 발화한 시점의 슬로우 쿼리·이벤트·동시 알림"
                            >
                              {impactOpenId === r.id ? "닫기" : "영향도"}
                            </button>
                          )}
                          {admin && (
                            <button
                              onClick={() => remove(r.id)}
                              className="text-rose-400 hover:text-rose-300 text-xs"
                            >
                              삭제
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                    {impactOpenId === r.id && (
                      <tr className="bg-zinc-950/40">
                        <td colSpan={6} className="px-3 py-4">
                          <ImpactPanel
                            loading={impactLoading}
                            error={impactError}
                            data={impactData}
                          />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </PageBody>
  );
}

// ---------------------------------------------------------------------------
// Slack ack setup guide — collapsible, persists state to localStorage.
//
// The Slack interactive Lambda is wired in CDK but the workspace-side setup
// (create Slack app + paste signing secret + set Request URL) can only be
// done by the user. This panel walks them through the four steps with a
// copy-button for the API Gateway endpoint URL discovered from the runtime
// /config.json, so they don't have to dig into the deploy outputs.
// ---------------------------------------------------------------------------

const SLACK_GUIDE_KEY = "dbops_slack_guide_open";

function SlackAckSetupGuide() {
  const [open, setOpen] = useState(false);
  const [endpoint, setEndpoint] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      setOpen(window.localStorage.getItem(SLACK_GUIDE_KEY) === "1");
    } catch {
      /* ignore */
    }
  }, []);

  // Resolve the live API Gateway URL once the panel is opened so users see
  // the actual prefilled path they need to paste into their Slack app.
  useEffect(() => {
    if (!open || endpoint) return;
    apiUrl("/api/slack/interactive")
      .then((u) => setEndpoint(u))
      .catch(() => setEndpoint("(unable to resolve — check /config.json)"));
  }, [open, endpoint]);

  const toggle = () => {
    setOpen((p) => {
      const next = !p;
      try {
        window.localStorage.setItem(SLACK_GUIDE_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  const copyEndpoint = async () => {
    if (!endpoint) return;
    try {
      await navigator.clipboard.writeText(endpoint);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 클립보드 API가 차단된 환경(비-HTTPS·권한 거부) — 조용히 실패하지
      // 않고 직접 선택해 복사하라고 안내한다. 주소는 위 code 블록에서
      // 선택 가능하다.
      setCopyFailed(true);
      setTimeout(() => setCopyFailed(false), 4000);
    }
  };

  return (
    <Section
      eyebrow="integration"
      title="Slack 양방향 Ack 설정"
      description="Slack 알림 메시지의 ✓ Ack 버튼을 활성화하려면 Slack 앱 측 설정이 한 번 필요합니다."
      actions={
        <button
          type="button"
          onClick={toggle}
          className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-300 hover:border-amber-500 hover:text-amber-300 transition-colors font-mono"
        >
          {open ? "× 가이드 닫기" : "셋업 가이드 열기"}
        </button>
      }
    >
      {open && (
        <div className="border border-zinc-800 bg-zinc-900/40 p-5 space-y-4">
          <ol className="space-y-3">
            <GuideStep
              num={1}
              title="Slack 앱 생성"
              body={
                <>
                  <a
                    href="https://api.slack.com/apps?new_app=1"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-amber-300 hover:text-amber-200 underline underline-offset-2"
                  >
                    api.slack.com/apps
                  </a>
                  에서{" "}
                  <span className="text-zinc-200 font-mono">From scratch</span>
                  로 새 앱 생성 → 워크스페이스 선택.
                </>
              }
            />
            <GuideStep
              num={2}
              title="Signing Secret 복사"
              body={
                <>
                  앱 페이지 좌측 메뉴 →{" "}
                  <span className="text-zinc-200 font-mono">
                    Basic Information
                  </span>{" "}
                  →{" "}
                  <span className="text-zinc-200 font-mono">
                    App Credentials
                  </span>{" "}
                  섹션의{" "}
                  <span className="text-zinc-200 font-mono">
                    Signing Secret
                  </span>
                  을 복사해서{" "}
                  <span className="text-zinc-200 font-mono">
                    cdk/config/settings.py
                  </span>
                  의{" "}
                  <span className="text-zinc-200 font-mono">
                    SLACK_SIGNING_SECRET
                  </span>
                  에 붙여넣기.
                </>
              }
            />
            <GuideStep
              num={3}
              title="Interactivity Request URL 등록"
              body={
                <>
                  <div>
                    좌측 메뉴 →{" "}
                    <span className="text-zinc-200 font-mono">
                      Interactivity & Shortcuts
                    </span>
                    를 켜고{" "}
                    <span className="text-zinc-200 font-mono">Request URL</span>
                    에 아래 주소를 붙여넣기:
                  </div>
                  <div className="mt-2 flex items-center gap-2 bg-zinc-950 border border-zinc-700 px-3 py-2">
                    <code className="flex-1 text-xs font-mono text-amber-300 break-all">
                      {endpoint ?? "(URL 로딩 중…)"}
                    </code>
                    <button
                      type="button"
                      onClick={copyEndpoint}
                      disabled={!endpoint}
                      className="text-[10px] uppercase tracking-wider px-2 py-1 border border-zinc-700 text-zinc-300 hover:border-amber-500 hover:text-amber-300 disabled:opacity-40 transition-colors shrink-0"
                    >
                      {copied ? "✓ 복사됨" : "복사"}
                    </button>
                  </div>
                  {copyFailed && (
                    <div className="mt-1.5 text-[11px] text-amber-300">
                      클립보드 접근이 차단되었습니다 — 위 주소를 직접 선택해
                      복사하세요.
                    </div>
                  )}
                </>
              }
            />
            <GuideStep
              num={4}
              title="Incoming Webhook 추가 + 재배포"
              body={
                <>
                  좌측 메뉴 →{" "}
                  <span className="text-zinc-200 font-mono">
                    Incoming Webhooks
                  </span>
                  를 켜고 채널 webhook URL 발급 → 위{" "}
                  <span className="font-mono">Subscribers</span> 섹션에 protocol{" "}
                  <span className="font-mono">slack-webhook</span>으로 등록.
                  <br />
                  마지막으로{" "}
                  <span className="text-zinc-200 font-mono">
                    cdk deploy dbops-dev-agent
                  </span>
                  로 새 signing secret을 Lambda 환경에 반영.
                </>
              }
            />
          </ol>

          <div className="border-t border-zinc-800 pt-3 text-[11px] text-zinc-500">
            <span className="text-zinc-400 font-medium">동작 확인:</span> 알림이
            한 번 발사되면 Slack 메시지의{" "}
            <span className="font-mono">✓ Ack alert</span> 버튼을 누르세요. 위
            룰 테이블에{" "}
            <span className="px-1 py-0.5 bg-emerald-500/10 text-emerald-300 border border-emerald-500/40 text-[10px] font-mono">
              ✓ acked @user
            </span>{" "}
            배지가 즉시 표시되면 정상.
            <br />
            <span className="text-zinc-400 font-medium">트러블슈팅:</span> 버튼
            클릭 시{" "}
            <span className="font-mono">
              SLACK_SIGNING_SECRET not configured
            </span>{" "}
            메시지가 뜨면 2~4단계 중 한 단계가 누락된 상태.
          </div>
        </div>
      )}
    </Section>
  );
}

function GuideStep({
  num,
  title,
  body,
}: {
  num: number;
  title: string;
  body: React.ReactNode;
}) {
  return (
    <li className="grid grid-cols-[28px_1fr] gap-3 items-baseline">
      <span className="text-xs font-mono text-amber-400 tabular-nums">
        {String(num).padStart(2, "0")}
      </span>
      <div>
        <div className="text-sm text-zinc-200 font-medium mb-1">{title}</div>
        <div className="text-[12px] text-zinc-400 leading-relaxed">{body}</div>
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Impact panel — what was going on at the moment a rule fired. Rendered
// inline below the table row when the DBA clicks "영향도". Three sections:
// top slow queries, concurrent ops events (RDS / backup / vacuum etc.),
// and sibling alerts (other rules that fired in the same window).
// ---------------------------------------------------------------------------

function ImpactPanel({
  loading,
  error,
  data,
}: {
  loading: boolean;
  error: string | null;
  data: AlertImpact | null;
}) {
  if (loading) {
    return <div className="text-xs text-zinc-500">불러오는 중…</div>;
  }
  if (error) {
    return <div className="text-xs text-rose-400">{error}</div>;
  }
  if (!data) return null;
  if (!data.window) {
    return (
      <div className="text-xs text-zinc-500">
        {data.info || "이 룰은 아직 발화 이력이 없습니다."}
      </div>
    );
  }
  const fmt = (iso?: string) => (iso ? new Date(iso).toLocaleString() : "—");
  return (
    <div className="space-y-4">
      <div className="text-[11px] text-zinc-500">
        기준 시각{" "}
        <span className="font-mono text-zinc-300">
          {fmt(data.window.center)}
        </span>{" "}
        · ±{data.window.minutes}분 윈도우
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
          Top slow queries
        </div>
        {data.top_slow_queries.length === 0 ? (
          <div className="text-[11px] text-zinc-500 px-2 py-1.5 border border-zinc-800">
            윈도우 안에 슬로우 쿼리 기록이 없습니다.
          </div>
        ) : (
          <div className="border border-zinc-800 divide-y divide-zinc-800">
            {data.top_slow_queries.map((q, i) => (
              <div key={q.query_hash || i} className="px-3 py-2">
                <div className="flex items-baseline justify-between gap-3 mb-1">
                  <div className="text-[10px] text-zinc-500 font-mono">
                    {(q.query_hash || "").slice(0, 12)}…
                  </div>
                  <div className="text-[11px] text-zinc-400 tabular-nums">
                    total{" "}
                    <span className="text-zinc-200">
                      {Math.round(Number(q.total_ms) || 0)}ms
                    </span>{" "}
                    · {Number(q.calls) || 0} calls · mean{" "}
                    {Math.round(Number(q.mean_ms) || 0)}ms
                  </div>
                </div>
                <pre className="text-[11px] text-zinc-300 font-mono whitespace-pre-wrap break-all">
                  {(q.query_excerpt || "").trim()}
                </pre>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
          동시 이벤트
        </div>
        {data.concurrent_events.length === 0 ? (
          <div className="text-[11px] text-zinc-500 px-2 py-1.5 border border-zinc-800">
            윈도우 안에 운영 이벤트가 없습니다.
          </div>
        ) : (
          <div className="border border-zinc-800 divide-y divide-zinc-800">
            {data.concurrent_events.map((ev, i) => (
              <div
                key={i}
                className="px-3 py-2 flex items-baseline justify-between gap-3"
              >
                <div className="text-[11px] text-zinc-400">
                  <span className="font-mono text-zinc-200 mr-2">
                    {ev.event_type}
                  </span>
                  <span>{ev.message}</span>
                </div>
                <div className="text-[10px] text-zinc-600 tabular-nums">
                  {fmt(ev.event_time)}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
          동시 발화 알림 (cascading)
        </div>
        {data.concurrent_alerts.length === 0 ? (
          <div className="text-[11px] text-zinc-500 px-2 py-1.5 border border-zinc-800">
            같은 윈도우에 다른 알림은 없었습니다.
          </div>
        ) : (
          <div className="border border-zinc-800 divide-y divide-zinc-800">
            {data.concurrent_alerts.map((a, i) => (
              <div
                key={i}
                className="px-3 py-2 flex items-baseline justify-between gap-3"
              >
                <div className="text-[11px] text-zinc-300">
                  <span className="font-mono text-zinc-500 mr-2">
                    rule#{a.rule_id}
                  </span>
                  <span>{a.message}</span>
                </div>
                <div className="text-[10px] text-zinc-600 tabular-nums">
                  {fmt(a.event_time)}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
