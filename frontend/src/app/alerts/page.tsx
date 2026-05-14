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
import { PageHeader, PageBody } from "@/components/design-system/page-shell";
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
        <div className="bg-red-900/30 border border-red-700 rounded-lg p-4 text-red-300 text-sm mb-4">
          {err}
        </div>
      )}

      {!admin && (
        <div className="mb-6 px-3 py-2 border border-zinc-800 text-[11px] uppercase tracking-wider text-zinc-500">
          read-only · viewer — write actions are hidden
        </div>
      )}

      {admin && (
        <form
          onSubmit={submit}
          className="bg-zinc-800 border border-zinc-700 rounded-lg p-4 mb-6 grid grid-cols-1 md:grid-cols-6 gap-3 items-end"
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
              className="w-full bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm rounded px-2 py-1.5 mt-1"
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
              className="w-full bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm rounded px-2 py-1.5 mt-1"
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
              className="w-full bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm rounded px-2 py-1.5 mt-1"
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
              className="w-full bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm rounded px-2 py-1.5 mt-1"
            />
          </div>
          <button
            type="submit"
            className="bg-sky-600 hover:bg-sky-500 text-white text-sm rounded px-4 py-1.5 transition"
          >
            Add Rule
          </button>
        </form>
      )}

      <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-4 mb-6">
        <div className="flex items-baseline justify-between mb-3">
          <div>
            <div className="text-xs text-zinc-400 uppercase tracking-wider">
              Notification Subscribers
            </div>
            <div className="text-[11px] text-zinc-500 mt-0.5">
              SNS topic:{" "}
              <span className="font-mono text-zinc-400">
                {topicArn || "(not configured)"}
              </span>
            </div>
          </div>
        </div>
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
                className="w-full bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm rounded px-2 py-1.5 mt-1"
              >
                <option value="email">Email</option>
                <option value="sms">SMS</option>
                <option value="https">HTTPS webhook</option>
                <option value="slack-webhook">Slack incoming webhook</option>
                <option value="pagerduty-events-v2">PagerDuty events-v2</option>
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
                className="w-full bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm rounded px-2 py-1.5 mt-1"
              />
            </div>
            <button
              type="submit"
              className="bg-emerald-600 hover:bg-emerald-500 text-white text-sm rounded px-4 py-1.5 transition"
            >
              Subscribe
            </button>
          </form>
        )}
        {subs.length > 0 ? (
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/50 border-y border-zinc-700">
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
            <tbody className="divide-y divide-zinc-700">
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
                        className={`px-1.5 py-0.5 rounded text-[10px] ${
                          pending
                            ? "bg-amber-500/20 text-amber-400"
                            : "bg-emerald-500/20 text-emerald-400"
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
            no subscribers yet. Add email/SMS/Slack webhook to receive triggered
            alerts.
          </div>
        )}
      </div>

      {loading ? (
        <div className="text-zinc-500 text-sm">Loading...</div>
      ) : rules.length === 0 ? (
        <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-8 text-center text-zinc-500">
          no alert rules yet — add one above
        </div>
      ) : (
        <div className="bg-zinc-800 border border-zinc-700 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-zinc-900/50 border-b border-zinc-700">
              <tr>
                <th className="text-left px-3 py-2 text-zinc-400 font-medium">
                  Status
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
            <tbody className="divide-y divide-zinc-700">
              {rules.map((r) => (
                <tr key={r.id} className="hover:bg-zinc-900/40">
                  <td className="px-3 py-2">
                    <button
                      onClick={() => toggle(r.id, r.enabled)}
                      disabled={!admin}
                      className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                        r.enabled
                          ? "bg-emerald-500/20 text-emerald-400 hover:bg-emerald-500/30"
                          : "bg-zinc-700 text-zinc-400 hover:bg-zinc-600"
                      } ${admin ? "" : "cursor-not-allowed opacity-70"}`}
                    >
                      {r.enabled ? "enabled" : "disabled"}
                    </button>
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
    </PageBody>
  );
}
