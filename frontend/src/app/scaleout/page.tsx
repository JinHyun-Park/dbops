"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  cancelScaleoutOp,
  fetchScaleoutOps,
  fetchTopology,
  scaleoutAz,
  type ScaleoutAzResult,
  type ScaleoutOp,
} from "@/lib/api-client";
import { isAdmin } from "@/lib/auth";
import {
  EmptyState,
  PageBody,
  PageHeader,
  Section,
} from "@/components/design-system/page-shell";
import { fmtRelative } from "@/lib/format";
import { useSelectedCluster } from "@/lib/use-selected-cluster";
import { engineFamily } from "@/lib/engine";

// State strings come straight from the API's derived lifecycle; the badge
// palette matches the operational severity (provisioning=neutral, awaiting
// approval=amber, warming=in-progress sky, warmed=success emerald, failed=red).
const STATE_STYLE: Record<string, string> = {
  reader_provisioning: "bg-zinc-700/30 text-zinc-300 border-zinc-600",
  warm_pending_approval: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  warm_approved: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40",
  warming: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  warmed: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  cancelled: "bg-zinc-800/40 text-zinc-500 border-zinc-700",
  provision_failed: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  warm_failed: "bg-rose-500/15 text-rose-300 border-rose-500/40",
};
const STATE_LABEL: Record<string, string> = {
  reader_provisioning: "리더 생성 중",
  warm_pending_approval: "예열 승인 대기",
  warm_approved: "예열 승인됨",
  warming: "예열 중",
  warmed: "예열 완료",
  cancelled: "취소됨",
  provision_failed: "리더 생성 실패",
  warm_failed: "예열 실패",
};
// Only ops that haven't dispatched the warm yet can be cancelled.
const CANCELLABLE = new Set(["reader_provisioning", "warm_pending_approval"]);

function isoFromMs(ms: string | undefined): string | undefined {
  if (!ms) return undefined;
  const n = Number(ms);
  return Number.isFinite(n) ? new Date(n).toISOString() : undefined;
}

export default function ScaleoutPage() {
  const [ops, setOps] = useState<ScaleoutOp[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [admin, setAdmin] = useState(false);
  // Per-row inline confirm (no browser confirm() dialog) + in-flight guard.
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  useEffect(() => {
    setAdmin(isAdmin());
  }, []);

  const load = useCallback(() => {
    setErr(null);
    fetchScaleoutOps()
      .then((d) => setOps(d.ops || []))
      .catch((e) => {
        setOps([]);
        setErr(e instanceof Error ? e.message : "조회 실패");
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Poll while anything is mid-flight so the DBA watches provisioning → warming
  // → warmed advance without a manual refresh.
  const inFlight = ops.some((o) =>
    [
      "reader_provisioning",
      "warm_pending_approval",
      "warm_approved",
      "warming",
    ].includes(o.state),
  );
  useEffect(() => {
    if (!inFlight) return;
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [inFlight, load]);

  const doCancel = useCallback(
    async (id: string) => {
      setCancellingId(id);
      setActionMsg(null);
      try {
        const r = await cancelScaleoutOp(id);
        setActionMsg(r.note || "취소되었습니다.");
        load();
      } catch (e) {
        setActionMsg(e instanceof Error ? e.message : String(e));
      } finally {
        setCancellingId(null);
        setConfirmingId(null);
      }
    },
    [load],
  );

  return (
    <PageBody>
      <PageHeader
        eyebrow="자동화"
        title="스케일 관리"
        description="리더 추가(scale-out)와 자동 버퍼풀 예열 작업의 진행 상태입니다. 예열이 시작되기 전(리더 생성 중·승인 대기)인 작업은 취소할 수 있습니다."
        actions={
          <button
            onClick={load}
            className="text-xs px-3 py-2 border border-zinc-700 text-zinc-400 hover:text-amber-300 hover:border-amber-500/40 transition-colors"
          >
            새로고침
          </button>
        }
      />

      {admin && <AzScaleoutRunbook />}

      <Section>
        <p className="text-xs text-zinc-500 mb-4 leading-relaxed">
          다른 예열 설정(top_n·엔드포인트)을 원하면 이 작업을 취소한 뒤 채팅에서
          prewarm_reader로 재요청하세요 — 리더는 유지됩니다.
        </p>

        {actionMsg && (
          <div className="text-xs text-zinc-400 mb-3 border-l-2 border-zinc-700 pl-3">
            {actionMsg}
          </div>
        )}

        {err ? (
          <div className="bg-rose-500/10 border border-rose-500/30 rounded-lg p-4 text-sm">
            <div className="text-rose-300 font-medium mb-1">
              스케일 작업을 불러오지 못했습니다
            </div>
            <div className="text-zinc-400">
              {err} — 네트워크 또는 인증 문제일 수 있습니다. 빈 목록이 아니라
              조회 실패 상태입니다.
            </div>
            <button
              onClick={load}
              className="mt-2 rounded border border-zinc-700 px-3 py-1 text-zinc-200 hover:bg-zinc-800"
            >
              다시 시도
            </button>
          </div>
        ) : loading ? (
          <div className="text-zinc-500 text-sm py-8">불러오는 중…</div>
        ) : ops.length === 0 ? (
          <EmptyState
            eyebrow="스케일 관리"
            title="진행 중인 스케일 작업이 없습니다"
            description="채팅에서 scale_out_with_warmup로 리더를 추가하면 여기에 진행 상태가 표시됩니다."
            secondary={{ href: "/chat", label: "Agent에게 물어보기" }}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wider text-zinc-500 border-b border-zinc-800">
                  <th className="py-2 pr-4 font-medium">클러스터</th>
                  <th className="py-2 pr-4 font-medium">리더 인스턴스</th>
                  <th className="py-2 pr-4 font-medium">상태</th>
                  <th className="py-2 pr-4 font-medium">엔드포인트</th>
                  <th className="py-2 pr-4 font-medium">top_n</th>
                  <th className="py-2 pr-4 font-medium">생성</th>
                  <th className="py-2 pr-2 font-medium text-right">액션</th>
                </tr>
              </thead>
              <tbody>
                {ops.map((op) => {
                  const cancellable = admin && CANCELLABLE.has(op.state);
                  return (
                    <tr
                      key={op.approval_id}
                      className="border-b border-zinc-900 hover:bg-zinc-900/40 transition-colors"
                    >
                      <td className="py-2.5 pr-4 font-mono text-zinc-300">
                        {op.cluster_id}
                      </td>
                      <td className="py-2.5 pr-4 font-mono text-zinc-400">
                        {op.reader_instance_id || "—"}
                      </td>
                      <td className="py-2.5 pr-4">
                        <span
                          className={`inline-block px-2 py-0.5 text-[10px] font-mono uppercase tracking-wider border ${
                            STATE_STYLE[op.state] ||
                            STATE_STYLE.reader_provisioning
                          }`}
                        >
                          {STATE_LABEL[op.state] || op.state}
                        </span>
                      </td>
                      <td className="py-2.5 pr-4 font-mono text-zinc-500 text-xs">
                        {op.endpoint_identifier || "—"}
                      </td>
                      <td className="py-2.5 pr-4 font-mono text-zinc-400">
                        {op.top_n ?? "—"}
                      </td>
                      <td className="py-2.5 pr-4 text-[11px] text-zinc-500 font-mono whitespace-nowrap">
                        {fmtRelative(isoFromMs(op.created_at))}
                      </td>
                      <td className="py-2.5 pr-2 text-right whitespace-nowrap">
                        {cancellable ? (
                          confirmingId === op.approval_id ? (
                            <span className="inline-flex items-center gap-1.5">
                              <span className="text-[11px] text-zinc-500">
                                취소할까요?
                              </span>
                              <button
                                onClick={() => doCancel(op.approval_id)}
                                disabled={cancellingId === op.approval_id}
                                className="text-[11px] px-2 py-1 border border-rose-500/40 text-rose-300 hover:bg-rose-500/10 transition-colors disabled:opacity-50"
                              >
                                {cancellingId === op.approval_id ? "…" : "확인"}
                              </button>
                              <button
                                onClick={() => setConfirmingId(null)}
                                disabled={cancellingId === op.approval_id}
                                className="text-[11px] px-2 py-1 border border-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors disabled:opacity-50"
                              >
                                아니오
                              </button>
                            </span>
                          ) : (
                            <button
                              onClick={() => setConfirmingId(op.approval_id)}
                              className="text-[11px] px-2 py-1 border border-zinc-700 text-zinc-400 hover:text-rose-300 hover:border-rose-500/40 transition-colors"
                            >
                              취소
                            </button>
                          )
                        ) : (
                          <span className="text-zinc-700 text-xs">—</span>
                        )}
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

// AZ 스케일아웃 (선점) — admin-only runbook. Adds N readers spread over the
// cluster's healthy AZs, EXCLUDING one chosen AZ, as individually-approved
// add_reader_instance requests. Preemptive spread away from an at-risk AZ.
function AzScaleoutRunbook() {
  const { clusters } = useSelectedCluster();
  const relational = useMemo(
    () => clusters.filter((c) => engineFamily(c.engine) === "relational"),
    [clusters],
  );

  const [clusterId, setClusterId] = useState("");
  const [excludeAz, setExcludeAz] = useState("");
  const [count, setCount] = useState(2);
  const [azs, setAzs] = useState<string[]>([]);
  const [azLoading, setAzLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<ScaleoutAzResult | null>(null);

  // Default to the first relational cluster once the list loads.
  useEffect(() => {
    if (!clusterId && relational.length) setClusterId(relational[0].cluster_id);
  }, [relational, clusterId]);

  // Populate the exclude-AZ dropdown from the cluster's current instance AZs
  // (the AZs you'd want to steer away from). The backend spreads over the
  // cluster's subnet AZs minus the excluded one and validates the choice.
  useEffect(() => {
    if (!clusterId) return;
    let cancelled = false;
    setAzLoading(true);
    setExcludeAz("");
    setAzs([]);
    fetchTopology(clusterId)
      .then((t) => {
        if (cancelled) return;
        const distinct = Array.from(
          new Set(
            (t.members || [])
              .map((m) => m.availability_zone)
              .filter((a): a is string => !!a),
          ),
        ).sort();
        setAzs(distinct);
      })
      .catch(() => !cancelled && setAzs([]))
      .finally(() => !cancelled && setAzLoading(false));
    return () => {
      cancelled = true;
    };
  }, [clusterId]);

  const submit = useCallback(async () => {
    if (!clusterId) return;
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      const r = await scaleoutAz({
        cluster_id: clusterId,
        exclude_az: excludeAz || undefined,
        count,
      });
      setResult(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [clusterId, excludeAz, count]);

  return (
    <Section>
      <div className="mb-4">
        <h2 className="text-sm font-medium text-zinc-200">
          AZ 스케일아웃 (선점)
        </h2>
        <p className="text-xs text-zinc-500 mt-1 leading-relaxed">
          정상 AZ에 리더를 미리 분산 배치합니다 — 위험이 예상되는 AZ 하나를
          제외하고, 나머지 AZ에 리더 {count}대를 라운드로빈으로 계획합니다. 각
          리더는 과금 대상인 개별 인스턴스이며, 승인 센터에서 하나씩
          검토·승인해야 실제로 생성됩니다.
        </p>
      </div>

      {relational.length === 0 ? (
        <EmptyState
          eyebrow="AZ 스케일아웃"
          title="Aurora 클러스터가 없습니다"
          description="AZ 스케일아웃은 Aurora PostgreSQL/MySQL 클러스터 전용입니다."
        />
      ) : (
        <>
          <div className="flex flex-wrap items-end gap-4">
            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                클러스터
              </span>
              <select
                value={clusterId}
                onChange={(e) => setClusterId(e.target.value)}
                className="bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5 min-w-[16rem] focus:border-amber-500/50 focus:outline-none"
              >
                {relational.map((c) => (
                  <option key={c.cluster_id} value={c.cluster_id}>
                    {c.cluster_id}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                제외할 AZ
              </span>
              <select
                value={excludeAz}
                onChange={(e) => setExcludeAz(e.target.value)}
                disabled={azLoading}
                className="bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5 min-w-[12rem] focus:border-amber-500/50 focus:outline-none disabled:opacity-50"
              >
                <option value="">
                  {azLoading ? "AZ 불러오는 중…" : "(제외 없음)"}
                </option>
                {azs.map((az) => (
                  <option key={az} value={az}>
                    {az}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-[10px] uppercase tracking-wider text-zinc-500">
                리더 수 (1–10)
              </span>
              <input
                type="number"
                min={1}
                max={10}
                value={count}
                onChange={(e) =>
                  setCount(
                    Math.max(1, Math.min(10, Number(e.target.value) || 1)),
                  )
                }
                className="bg-zinc-900 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5 w-24 focus:border-amber-500/50 focus:outline-none"
              />
            </label>

            <button
              onClick={submit}
              disabled={busy || !clusterId}
              className="text-xs px-4 py-2 border border-amber-500/40 text-amber-300 hover:bg-amber-500/10 transition-colors disabled:opacity-50"
            >
              {busy ? "생성 중…" : "리더 추가 승인 요청 생성"}
            </button>
          </div>

          {err && (
            <div className="mt-4 bg-rose-500/10 border border-rose-500/30 rounded-lg p-3 text-sm">
              <span className="text-rose-300">{err}</span>
            </div>
          )}

          {result && (
            <div className="mt-4 border border-zinc-800 rounded-lg p-4">
              <div className="text-sm text-zinc-200 mb-2">{result.message}</div>
              {result.instance_class && (
                <div className="text-xs text-zinc-500 mb-2 font-mono">
                  클래스 {result.instance_class}
                  {result.healthy_azs?.length
                    ? ` · 대상 AZ ${result.healthy_azs.join(", ")}`
                    : ""}
                </div>
              )}
              {result.created.length > 0 && (
                <ul className="text-xs text-zinc-300 space-y-1 mb-2">
                  {result.created.map((c) => (
                    <li key={c.approval_id} className="font-mono">
                      <span className="text-emerald-400">✓</span>{" "}
                      {c.new_instance_id}{" "}
                      <span className="text-zinc-500">
                        ({c.availability_zone})
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              {result.failed.length > 0 && (
                <ul className="text-xs text-rose-300 space-y-1 mb-2">
                  {result.failed.map((f, i) => (
                    <li key={i} className="font-mono">
                      <span>✗</span> {f.new_instance_id} — {f.reason}
                    </li>
                  ))}
                </ul>
              )}
              <a
                href="/approvals"
                className="text-xs text-amber-300 hover:underline"
              >
                승인 센터에서 검토·승인 →
              </a>
            </div>
          )}
        </>
      )}
    </Section>
  );
}
