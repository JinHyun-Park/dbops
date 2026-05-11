"use client";

import { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import { fetchClusters, registerCluster } from "@/lib/api-client";
import { PageHeader, PageBody, EmptyState, Section } from "@/components/design-system/page-shell";

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
}

const CONN_STYLES: Record<string, { label: string; classes: string }> = {
  ok: { label: "ok", classes: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30" },
  failed: { label: "failed", classes: "bg-rose-500/10 text-rose-400 border-rose-500/30" },
  untested: { label: "untested", classes: "bg-zinc-700/40 text-zinc-400 border-zinc-700" },
};

const STATUS_STYLES: Record<string, string> = {
  available: "text-emerald-400",
  "backing-up": "text-amber-400",
  modifying: "text-amber-400",
  stopped: "text-rose-400",
  failed: "text-rose-400",
};

function relTime(iso?: string): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

export default function ClustersPage() {
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({
    cluster_id: "",
    account_id: "",
    region: "ap-northeast-2",
    engine: "aurora-postgresql",
    spoke_role_arn: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "ok" | "warn" | "err"; msg: string } | null>(null);

  const loadClusters = useCallback(() => {
    fetchClusters().then(setClusters).catch(console.error);
  }, []);

  useEffect(() => {
    loadClusters();
  }, [loadClusters]);

  const handleRegister = async () => {
    if (!form.cluster_id || !form.account_id || !form.region) {
      setFeedback({ kind: "err", msg: "cluster_id / account_id / region 모두 필요합니다." });
      return;
    }
    setSubmitting(true);
    setFeedback(null);
    try {
      const result = await registerCluster(form);
      const status = result?.connection_status;
      if (status === "ok") {
        setFeedback({ kind: "ok", msg: `등록됨: ${form.cluster_id} · 연결 검증 통과` });
      } else if (status === "failed") {
        setFeedback({
          kind: "warn",
          msg: `등록은 됐지만 연결 검증 실패: ${result?.connection_error || "AWS console 확인"}`,
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
      setFeedback({ kind: "err", msg: e instanceof Error ? e.message : "Failed" });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <PageBody>
      <PageHeader
        eyebrow="configuration"
        title="Cluster registry"
        description="Aurora 클러스터 등록과 cross-account 연결 관리. 메트릭/실시간 상태는 Fleet 또는 Dashboard에서 확인하세요."
        actions={
          <>
            <Link
              href="/fleet"
              className="text-xs px-3 py-2 border border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
            >
              Fleet overview →
            </Link>
            <button
              onClick={() => setShowForm(!showForm)}
              className="text-xs font-medium px-3 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
            >
              {showForm ? "취소" : "+ Register cluster"}
            </button>
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

      {showForm && (
        <Section eyebrow="new registration" title="Register an Aurora cluster">
          <div className="border border-zinc-800 bg-zinc-900/40 p-6">
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
              <Field
                label="Spoke Role ARN (cross-account)"
                value={form.spoke_role_arn}
                onChange={(v) => setForm({ ...form, spoke_role_arn: v })}
                placeholder="arn:aws:iam::<account>:role/dbops-spoke-role (optional)"
                mono
                fullWidth
              />
            </div>
            <p className="text-[11px] text-zinc-500 mt-4 leading-relaxed">
              same-account 클러스터는 spoke role을 비워 두세요. cross-account 등록 시 STS
              AssumeRole + <span className="font-mono text-zinc-400">rds:DescribeDBClusters</span>로
              연결을 검증한 뒤 저장합니다.
            </p>
            <div className="flex gap-2 mt-5">
              <button
                disabled={submitting}
                onClick={handleRegister}
                className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:bg-zinc-700 disabled:text-zinc-500 transition-colors"
              >
                {submitting ? "검증 중…" : "등록 + 연결 검증"}
              </button>
              <button
                onClick={() => setShowForm(false)}
                className="text-xs px-4 py-2 border border-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
              >
                취소
              </button>
            </div>
          </div>
        </Section>
      )}

      <Section
        eyebrow="registered"
        title={`${clusters.length} cluster${clusters.length === 1 ? "" : "s"}`}
        description="이 페이지는 등록/검증/관리 전용입니다. 실시간 메트릭은 Fleet 또는 Dashboard에서."
      >
        {clusters.length === 0 ? (
          <EmptyState
            eyebrow="no clusters"
            title="Register your first Aurora cluster"
            description="Cluster ID, account, region을 입력하면 RDS Data API 기반 메트릭 수집이 시작됩니다."
            primary={{ onClick: () => setShowForm(true), label: "+ Register cluster" }}
            secondary={{ href: "/chat", label: "Ask the agent first" }}
          />
        ) : (
          <div className="border border-zinc-800 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-zinc-900/60 text-[10px] uppercase tracking-wider text-zinc-500">
                <tr>
                  <th className="text-left px-4 py-2.5 font-medium">cluster</th>
                  <th className="text-left px-4 py-2.5 font-medium">engine</th>
                  <th className="text-left px-4 py-2.5 font-medium">account · region</th>
                  <th className="text-left px-4 py-2.5 font-medium">status</th>
                  <th className="text-left px-4 py-2.5 font-medium">connection</th>
                  <th className="text-left px-4 py-2.5 font-medium">registered</th>
                  <th className="text-right px-4 py-2.5 font-medium">actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800">
                {clusters.map((c) => {
                  const conn = c.connection_status || "untested";
                  const connStyle = CONN_STYLES[conn] || CONN_STYLES.untested;
                  const statusColor = STATUS_STYLES[c.status || ""] || "text-zinc-500";
                  return (
                    <tr key={c.cluster_id} className="hover:bg-zinc-900/40">
                      <td className="px-4 py-2.5">
                        <div className="text-zinc-100 font-mono text-xs">{c.cluster_id}</div>
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
                        <div className="text-zinc-300 text-xs">{c.engine || "—"}</div>
                        {c.engine_version && (
                          <div className="text-[10px] text-zinc-500 font-mono">{c.engine_version}</div>
                        )}
                      </td>
                      <td className="px-4 py-2.5 text-zinc-300 text-xs font-mono">
                        {c.account_id}
                        <div className="text-zinc-500 text-[10px]">{c.region}</div>
                      </td>
                      <td className={`px-4 py-2.5 text-xs ${statusColor}`}>{c.status || "—"}</td>
                      <td className="px-4 py-2.5">
                        <span
                          className={`px-1.5 py-0.5 border text-[10px] font-mono ${connStyle.classes}`}
                          title={c.connection_error || ""}
                        >
                          {connStyle.label}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-zinc-500 text-xs">
                        {relTime(c.registered_at)}
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <Link
                          href={`/dashboard?cluster=${encodeURIComponent(c.cluster_id)}`}
                          className="text-xs text-amber-400/90 hover:text-amber-300"
                        >
                          dashboard →
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </PageBody>
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
