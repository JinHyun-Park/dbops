"use client";

import { useState, useEffect, useCallback } from "react";
import { ApprovalCard } from "@/components/approval/approval-card";
import { apiUrl, authedFetch } from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";

export default function ApprovalsPage() {
  const [approvals, setApprovals] = useState<any[]>([]);
  const [filter, setFilter] = useState<"pending" | "approved" | "rejected">(
    "pending",
  );
  // 조회 실패를 빈 배열로 삼키면 장애가 "승인 요청 없음"으로 위장된다 —
  // 에러를 명시적으로 잡아 빈 상태와 구분한다(Codex 감사).
  const [loadError, setLoadError] = useState<string | null>(null);

  const loadApprovals = useCallback(() => {
    setLoadError(null);
    apiUrl(`/api/approvals?status=${filter}`)
      .then((url) => authedFetch(url))
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setApprovals)
      .catch((e) => {
        setApprovals([]);
        setLoadError(e instanceof Error ? e.message : "조회 실패");
      });
  }, [filter]);

  useEffect(() => {
    loadApprovals();
  }, [loadApprovals]);

  const handleAction = async (id: string, action: "approve" | "reject") => {
    const url = await apiUrl(`/api/approvals/${id}`);
    await authedFetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, approved_by: "dba" }),
    });
    loadApprovals();
  };

  return (
    <PageBody>
      <PageHeader
        eyebrow="자동화"
        title="승인 센터"
        description="Agent 또는 대시보드가 제안한 변경 작업(DDL, parameter, scaling, maintenance, snapshot/restore, Data API 활성화)을 DBA가 검토하고 승인하는 게이트입니다. 승인됨 탭에는 실행 완료(consumed)된 건도 함께 표시됩니다."
        actions={
          <div className="flex gap-1">
            {(["pending", "approved", "rejected"] as const).map((s) => (
              <button
                key={s}
                onClick={() => setFilter(s)}
                className={`text-xs px-3 py-2 transition-colors ${
                  filter === s
                    ? "bg-amber-500 text-zinc-950"
                    : "border border-zinc-700 text-zinc-400 hover:text-zinc-100"
                }`}
              >
                {s === "pending"
                  ? "승인 대기"
                  : s === "approved"
                    ? "승인됨"
                    : "거부됨"}
              </button>
            ))}
          </div>
        }
      />

      {loadError ? (
        <div className="bg-rose-500/10 border border-rose-500/30 rounded-lg p-4 text-sm">
          <div className="text-rose-300 font-medium mb-1">
            승인 목록을 불러오지 못했습니다
          </div>
          <div className="text-zinc-400">
            {loadError} — 네트워크 또는 인증 문제일 수 있습니다. 빈 목록이
            아니라 조회 실패 상태입니다.
          </div>
          <button
            onClick={loadApprovals}
            className="mt-2 rounded border border-zinc-700 px-3 py-1 text-zinc-200 hover:bg-zinc-800"
          >
            다시 시도
          </button>
        </div>
      ) : approvals.length === 0 ? (
        <EmptyState
          eyebrow={
            filter === "pending"
              ? "승인 대기"
              : filter === "approved"
                ? "승인됨"
                : "거부됨"
          }
          title={
            filter === "pending"
              ? "대기 중인 승인 요청이 없습니다"
              : filter === "approved"
                ? "아직 승인된 작업이 없습니다"
                : "거부된 작업이 없습니다"
          }
          description={
            filter === "pending"
              ? "Agent가 쓰기 작업을 제안하면 이 페이지에 검토 항목으로 올라옵니다."
              : undefined
          }
          secondary={{ href: "/chat", label: "Agent에게 물어보기" }}
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {approvals.map((a) => (
            <ApprovalCard
              key={a.approval_id}
              approval={a}
              onApprove={(id) => handleAction(id, "approve")}
              onReject={(id) => handleAction(id, "reject")}
            />
          ))}
        </div>
      )}
    </PageBody>
  );
}
