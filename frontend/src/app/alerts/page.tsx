"use client";

import { useEffect, useState } from "react";
import {
  fetchAlertRules,
  fetchClusters,
  createAlertRule,
  updateAlertRule,
  deleteAlertRule,
  fetchAlertSubscriptions,
  createAlertSubscription,
  deleteAlertSubscription,
} from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";
import { isAdmin } from "@/lib/auth";

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
      title="no metric snapshots ever recorded for this cluster + metric — check the cluster registration or ETL pipeline"
    >
      no data
    </span>
  );
}

const METRIC_OPTIONS = [
  "cpu",
  "aas",
  "connections",
  "conn_active",
  "deadlocks",
  "read_iops",
  "write_iops",
  "replica_lag_ms",
  "storage_bytes",
  "mem_free",
];

const COMP_OPS = [">", ">=", "<", "<=", "==", "!="] as const;

export default function AlertsPage() {
  const [rules, setRules] = useState<Rule[]>([]);
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
        if (cs.length > 0)
          setNewRule((r) => ({ ...r, cluster_id: cs[0].cluster_id }));
      })
      .catch((e) => setErr(`Clusters: ${e.message}`));
    reload();
    reloadSubs();
  }, []);

  const submitSub = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newSub.endpoint) return;
    try {
      await createAlertSubscription(newSub.protocol, newSub.endpoint);
      setNewSub({ protocol: "email", endpoint: "" });
      reloadSubs();
    } catch (err) {
      setErr(err instanceof Error ? err.message : "Subscription failed");
    }
  };

  const removeSub = async (arn: string) => {
    await deleteAlertSubscription(arn);
    reloadSubs();
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newRule.cluster_id) return;
    try {
      await createAlertRule(newRule);
      setNewRule((r) => ({ ...r, name: "" }));
      reload();
    } catch (err) {
      setErr(err instanceof Error ? err.message : "Failed");
    }
  };

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
        eyebrow="configure"
        title="Alert rules"
        description={`${rules.length} rule${
          rules.length === 1 ? "" : "s"
        } · evaluated every 5 minutes against metric_snapshots`}
      />

      {err && (
        <div className="mb-6 px-4 py-3 border bg-rose-500/10 border-rose-500/30 text-rose-300 text-sm">
          {err}
        </div>
      )}

      {!admin && (
        <div className="mb-6 px-3 py-2 border border-zinc-800 text-[11px] uppercase tracking-wider text-zinc-500">
          read-only · viewer — write actions are hidden
        </div>
      )}

      {admin && (
        <Section eyebrow="new rule" title="Define an alert threshold">
          <form
            onSubmit={submit}
            className="border border-zinc-800 bg-zinc-900/40 p-6 grid grid-cols-1 md:grid-cols-6 gap-3 items-end"
          >
            <div className="md:col-span-2">
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
                  setNewRule({ ...newRule, threshold: Number(e.target.value) })
                }
                className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60 transition-colors"
              />
            </div>
            <button
              type="submit"
              className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
            >
              Add rule
            </button>
          </form>
        </Section>
      )}

      <Section
        eyebrow="notifications"
        title="Subscribers"
        description={
          topicArn
            ? `Fan-out via SNS topic ${topicArn}`
            : "SNS topic not configured"
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
                          : newSub.protocol === "pagerduty-events-v2"
                            ? "PagerDuty integration key (32 hex chars)"
                            : "https://example.com/webhook"
                  }
                  className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-sm px-3 py-2 mt-1 focus:outline-none focus:border-amber-500/60 transition-colors"
                />
              </div>
              <button
                type="submit"
                className="text-xs font-medium px-4 py-2 bg-emerald-500 text-zinc-950 hover:bg-emerald-400 transition-colors"
              >
                Subscribe
              </button>
            </form>
          )}
          {subs.length > 0 ? (
            <table className="w-full text-sm border border-zinc-800">
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
                          {pending ? "pending confirmation" : "confirmed"}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right">
                        {!pending && admin && (
                          <button
                            onClick={() => removeSub(s.subscription_arn)}
                            className="text-rose-400 hover:text-rose-300 text-xs"
                          >
                            unsubscribe
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <div className="text-zinc-500 text-xs py-2">
              no subscribers yet. Add email/SMS/Slack webhook to receive
              triggered alerts.
            </div>
          )}
        </div>
      </Section>

      <Section
        eyebrow="rules"
        title={`${rules.length} alert rule${rules.length === 1 ? "" : "s"}`}
        description="evaluator runs every 5 minutes; rules with stale or missing metrics are skipped."
      >
        {loading ? (
          <div className="text-zinc-500 text-sm">Loading...</div>
        ) : rules.length === 0 ? (
          <EmptyState
            eyebrow="no rules"
            title="Add your first alert rule"
            description="Pick a cluster + metric + threshold above. The evaluator runs every 5 minutes and fans out via SNS / Slack / PagerDuty subscribers."
          />
        ) : (
          <div className="border border-zinc-800 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-900/60 border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-500">
                <tr>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    Status
                  </th>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    Data
                  </th>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    Cluster
                  </th>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    Rule
                  </th>
                  <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                    Last Triggered
                  </th>
                  <th className="text-right px-3 py-2 text-zinc-400 font-medium">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800">
                {rules.map((r) => (
                  <tr key={r.id} className="hover:bg-zinc-900/40">
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
                        {r.enabled ? "enabled" : "disabled"}
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
                      {r.metric_type}{" "}
                      <span className="text-amber-400">{r.comparison}</span>{" "}
                      {r.threshold}
                    </td>
                    <td className="px-3 py-2 text-zinc-400 text-xs">
                      {r.last_triggered_at
                        ? new Date(r.last_triggered_at).toLocaleString()
                        : "never"}
                    </td>
                    <td className="px-3 py-2 text-right">
                      {admin && (
                        <button
                          onClick={() => remove(r.id)}
                          className="text-rose-400 hover:text-rose-300 text-xs"
                        >
                          delete
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </PageBody>
  );
}
