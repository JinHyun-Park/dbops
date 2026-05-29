"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchHealth, type HealthResponse } from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  Section,
} from "@/components/design-system/page-shell";
import { fmtBytes, fmtNumber } from "@/lib/format";

// Health dot — visual punchline per panel. Green if everything came
// back ok, amber if a section errored (degraded but the page still
// renders), rose if the section itself failed.
function statusColor(s: string | undefined): string {
  if (!s) return "bg-zinc-600";
  const lower = s.toLowerCase();
  if (lower === "available" || lower === "active") return "bg-emerald-400";
  if (lower.includes("modify") || lower.includes("creating")) {
    return "bg-amber-400";
  }
  return "bg-rose-400";
}

function relTime(ms: number): string {
  const diff = Date.now() - ms;
  if (diff < 60_000) return "방금";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}분 전`;
  return `${Math.floor(diff / 3_600_000)}시간 전`;
}

export default function HealthPage() {
  const [data, setData] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    fetchHealth()
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    // Auto-refresh every 30s — health state moves slowly + endpoint
    // is Cache-Control: 10s so the actual hit-rate is at most 3/min.
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <PageBody>
      <PageHeader
        eyebrow="self"
        title="DBOps health"
        description="DBOps 자체의 운영 상태 — Lambda 함수, Aurora cache, DynamoDB 테이블의 상태를 한 화면에. 30초마다 자동 새로고침."
        actions={
          <button
            onClick={load}
            disabled={loading}
            className="text-xs font-medium px-3 py-1.5 border border-zinc-700 text-zinc-300 hover:border-amber-500/60 hover:text-amber-200 transition-colors disabled:opacity-50"
          >
            {loading ? "확인 중…" : "새로고침"}
          </button>
        }
      />

      {error && (
        <div className="mb-4 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {error}
        </div>
      )}

      {data && (
        <div className="text-[11px] text-zinc-500 mb-6 font-mono">
          last check {relTime(data.checked_at)} · {data.elapsed_ms}ms aggregate
        </div>
      )}

      {/* Aurora cache */}
      <Section eyebrow="cache" title="Aurora PostgreSQL (cache DB)">
        {!data ? (
          <Loading />
        ) : data.aurora.error ? (
          <ErrorPanel msg={data.aurora.error} />
        ) : (
          <div className="border border-zinc-800 bg-zinc-900/40 p-4">
            <div className="flex items-baseline justify-between mb-3">
              <div className="flex items-center gap-2">
                <span
                  className={`w-2.5 h-2.5 rounded-full ${statusColor(
                    data.aurora.status,
                  )}`}
                />
                <span className="text-sm text-zinc-100 font-mono">
                  {data.aurora.cluster_id}
                </span>
                <span className="text-[11px] text-zinc-500">
                  · {data.aurora.status}
                </span>
              </div>
              <span className="text-[11px] text-zinc-500 font-mono">
                {data.aurora.engine} {data.aurora.engine_version}
              </span>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-[11px]">
              <Field label="endpoint" value={data.aurora.endpoint} mono />
              <Field
                label="Serverless v2 ACU"
                value={`${data.aurora.serverless_min_acu ?? "—"} ~ ${
                  data.aurora.serverless_max_acu ?? "—"
                }`}
              />
              <Field
                label="multi-AZ"
                value={data.aurora.multi_az ? "예" : "아니오"}
              />
              <Field
                label="deletion-protection"
                value={data.aurora.deletion_protection ? "켜짐" : "꺼짐"}
              />
            </div>
          </div>
        )}
      </Section>

      {/* DynamoDB */}
      <Section eyebrow="state store" title="DynamoDB tables">
        {!data ? (
          <Loading />
        ) : data.ddb.error ? (
          <ErrorPanel msg={data.ddb.error} />
        ) : (
          <div className="border border-zinc-800 divide-y divide-zinc-800">
            {(data.ddb.tables || []).map((t) => (
              <div
                key={t.name}
                className="px-4 py-3 flex items-baseline justify-between gap-3"
              >
                <div className="flex items-center gap-2 min-w-0">
                  <span
                    className={`w-2.5 h-2.5 rounded-full ${statusColor(
                      t.status,
                    )}`}
                  />
                  <span className="text-sm text-zinc-100 font-mono">
                    {t.label}
                  </span>
                  <span className="text-[11px] text-zinc-500 truncate">
                    · {t.name}
                  </span>
                </div>
                <div className="text-[11px] text-zinc-500 font-mono tabular-nums flex-shrink-0">
                  {t.error
                    ? t.error
                    : `${fmtNumber(t.item_count ?? 0)} rows · ${fmtBytes(
                        t.size_bytes ?? 0,
                      )}`}
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* Lambdas */}
      <Section
        eyebrow="compute"
        title="Lambda functions"
        description={
          data?.lambdas?.count !== undefined
            ? `${data.lambdas.active}/${data.lambdas.count} active`
            : ""
        }
      >
        {!data ? (
          <Loading />
        ) : data.lambdas.error ? (
          <ErrorPanel msg={data.lambdas.error} />
        ) : (
          <div className="border border-zinc-800 divide-y divide-zinc-800 max-h-[28rem] overflow-y-auto">
            {(data.lambdas.items || []).map((fn) => (
              <div
                key={fn.name}
                className="px-4 py-2.5 flex items-baseline justify-between gap-3"
              >
                <div className="flex items-center gap-2 min-w-0">
                  <span
                    className={`w-2 h-2 rounded-full ${statusColor(fn.state)}`}
                  />
                  <span className="text-xs text-zinc-100 font-mono truncate">
                    {fn.name.replace(/^dbops-[a-z]+-/, "")}
                  </span>
                </div>
                <div className="text-[10px] text-zinc-500 font-mono tabular-nums flex-shrink-0">
                  {fn.runtime} · {fn.memory_mb}MB · {fn.timeout_s}s
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>
    </PageBody>
  );
}

function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-0.5">
        {label}
      </div>
      <div className={`text-zinc-200 break-all ${mono ? "font-mono" : ""}`}>
        {value ?? "—"}
      </div>
    </div>
  );
}

function Loading() {
  return <div className="text-sm text-zinc-500">불러오는 중…</div>;
}

function ErrorPanel({ msg }: { msg: string }) {
  return (
    <div className="border border-rose-500/30 bg-rose-500/5 px-3 py-2 text-xs text-rose-300">
      {msg}
    </div>
  );
}
