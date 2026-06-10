"use client";

import { useEffect, useState } from "react";
import { apiUrl, authedFetch } from "@/lib/api-client";

// RDS Data API(HttpEndpoint) 비활성 클러스터의 경고 + 인앱 활성화 요청.
//
// "활성화 요청" 버튼은 직접 실행이 아니라 Approval Center에 승인 요청을
// 등록한다 — DBA가 승인하는 순간 approvals API가 rds:EnableHttpEndpoint
// (단일 액션, ModifyDBCluster 불필요)를 호출한다. 모든 변경은 사람이
// 승인한다는 DBOps 안전 모델을 그대로 따른다.
export function DataApiBanner({ clusterId }: { clusterId: string }) {
  const [phase, setPhase] = useState<
    "idle" | "submitting" | "pending" | "error"
  >("idle");
  const [error, setError] = useState<string | null>(null);

  // 이미 대기 중인 요청이 있으면 버튼 대신 "승인 대기 중"을 보여준다.
  // 조회 실패는 무시 — POST 경로가 서버에서 멱등(중복 pending 방지)이다.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const url = await apiUrl(`/api/approvals?status=pending`);
        const res = await authedFetch(url);
        const items = await res.json();
        if (
          alive &&
          Array.isArray(items) &&
          items.some(
            (a) =>
              a.action_type === "enable_data_api" && a.cluster_id === clusterId,
          )
        ) {
          setPhase("pending");
        }
      } catch {
        // ignore
      }
    })();
    return () => {
      alive = false;
    };
  }, [clusterId]);

  const request = async () => {
    setPhase("submitting");
    setError(null);
    try {
      const url = await apiUrl(`/api/approvals`);
      const res = await authedFetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cluster_id: clusterId,
          action_type: "enable_data_api",
          action_details: { cluster_id: clusterId },
          action_description: "RDS Data API(HttpEndpoint) 활성화",
          requested_by: "dashboard",
          risk_level: "medium",
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPhase("pending");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  };

  return (
    <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-4 text-sm">
      <div className="text-amber-300 font-medium mb-1">
        RDS Data API(HttpEndpoint) 비활성
      </div>
      <div className="text-zinc-300">
        CloudWatch 지표는 정상 수집되지만, 라이브 SQL 기반 패널(Vacuum &amp;
        Bloat, Table Sizes, Connection Activity, Top Queries, Configuration)과
        AI 에이전트의 SQL 실행은 이 클러스터에서 동작하지 않습니다. 다운타임
        없이 활성화할 수 있습니다 — 활성화 시 IAM 권한 기반으로 SQL 실행 경로가
        열립니다.
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        {phase === "pending" ? (
          <div className="flex items-center gap-2 text-amber-200">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
            활성화 승인 대기 중 —{" "}
            <a href="/approvals" className="underline hover:text-amber-100">
              Approval Center에서 검토
            </a>
          </div>
        ) : (
          <button
            onClick={request}
            disabled={phase === "submitting"}
            className="rounded bg-amber-500/20 border border-amber-500/40 px-3 py-1.5 text-amber-200 hover:bg-amber-500/30 disabled:opacity-50 transition-colors"
          >
            {phase === "submitting"
              ? "요청 등록 중…"
              : "활성화 요청 (DBA 승인 필요)"}
          </button>
        )}
        {phase === "error" && (
          <span className="text-rose-400 text-xs">요청 실패: {error}</span>
        )}
      </div>

      {/* CLI 직접 실행 경로(보조). Serverless v2·프로비저닝의 Data API는
          EnableHttpEndpoint(resource-arn 기반, CLI v2 전용)다 —
          modify-db-cluster의 --enable-http-endpoint는 legacy Serverless v1
          전용이며 그 외 클러스터에선 조용히 무시된다(실측 확인). */}
      <details className="mt-2">
        <summary className="cursor-pointer text-xs text-zinc-500 hover:text-zinc-400">
          CLI로 직접 활성화
        </summary>
        <code className="mt-1.5 block w-fit max-w-full overflow-x-auto rounded bg-zinc-900/80 px-2.5 py-1.5 font-mono text-xs text-amber-200">
          aws rds enable-http-endpoint --resource-arn
          arn:aws:rds:&lt;region&gt;:&lt;account&gt;:cluster:{clusterId}
        </code>
      </details>
    </div>
  );
}
