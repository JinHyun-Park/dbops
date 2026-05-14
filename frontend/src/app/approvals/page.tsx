"use client";

import { useState, useEffect, useCallback } from "react";
import { ApprovalCard } from "@/components/approval/approval-card";
import { apiUrl } from "@/lib/api-client";
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
      .then((url) => fetch(url))
      .then((r) => r.json())
      .then(setApprovals)
      .catch(console.error);
  }, [filter]);

  useEffect(() => {
    loadApprovals();
  }, [loadApprovals]);

  const handleAction = async (id: string, action: "approve" | "reject") => {
    const url = await apiUrl(`/api/approvals/${id}`);
    await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, approved_by: "dba" }),
    });
    loadApprovals();
  };

  return (
    <PageBody>
      <PageHeader
        eyebrow="automate"
        title="Approval center"
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
                  ? "pending"
                  : s === "approved"
                    ? "approved"
                    : "rejected"}
              </button>
            ))}
          </div>
        }
      />

      {approvals.length === 0 ? (
        <EmptyState
          eyebrow={filter}
          title={
            filter === "pending"
              ? "No pending approvals"
              : filter === "approved"
                ? "No approved actions yet"
                : "No rejected actions"
          }
          description={
            filter === "pending"
              ? "When the agent proposes a write action, it lands here for review."
              : undefined
          }
          secondary={{ href: "/chat", label: "Ask the agent" }}
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
