"use client";

import { useState, useEffect, useCallback } from "react";
import { ApprovalCard } from "@/components/approval/approval-card";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:3001";

export default function ApprovalsPage() {
  const [approvals, setApprovals] = useState<any[]>([]);
  const [filter, setFilter] = useState<"pending" | "approved" | "rejected">("pending");

  const loadApprovals = useCallback(() => {
    fetch(`${API_BASE}/api/approvals?status=${filter}`)
      .then((r) => r.json())
      .then(setApprovals)
      .catch(console.error);
  }, [filter]);

  useEffect(() => { loadApprovals(); }, [loadApprovals]);

  const handleAction = async (id: string, action: "approve" | "reject") => {
    await fetch(`${API_BASE}/api/approvals/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, approved_by: "dba" }),
    });
    loadApprovals();
  };

  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 p-6">
      <h1 className="text-2xl font-bold mb-6">Approval Center</h1>

      <div className="flex gap-2 mb-6">
        {(["pending", "approved", "rejected"] as const).map((s) => (
          <button
            key={s}
            onClick={() => setFilter(s)}
            className={`px-4 py-2 text-sm rounded-lg transition-colors ${
              filter === s ? "bg-blue-600 text-white" : "bg-zinc-800 text-zinc-400 hover:bg-zinc-700"
            }`}
          >
            {s === "pending" ? `대기 중` : s === "approved" ? "승인됨" : "거부됨"}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {approvals.length === 0 && (
          <div className="text-zinc-500 col-span-full text-center py-12">
            {filter === "pending" ? "대기 중인 승인 요청이 없습니다" : "항목이 없습니다"}
          </div>
        )}
        {approvals.map((a) => (
          <ApprovalCard
            key={a.approval_id}
            approval={a}
            onApprove={(id) => handleAction(id, "approve")}
            onReject={(id) => handleAction(id, "reject")}
          />
        ))}
      </div>
    </div>
  );
}
