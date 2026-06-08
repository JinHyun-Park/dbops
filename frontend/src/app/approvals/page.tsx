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

  const loadApprovals = useCallback(() => {
    apiUrl(`/api/approvals?status=${filter}`)
      .then((url) => authedFetch(url))
      .then((r) => r.json())
      .then(setApprovals)
      .catch(console.error);
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
        description="Agent가 제안한 쓰기 작업 (DDL, parameter, scaling, maintenance)을 DBA가 검토하고 승인하는 게이트입니다."
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

      {approvals.length === 0 ? (
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
