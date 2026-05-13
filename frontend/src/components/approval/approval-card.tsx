"use client";

interface Approval {
  approval_id: string;
  cluster_id: string;
  tool_name: string;
  action_description: string;
  risk_level: string;
  approval_status: string;
  requested_by: string;
  created_at: string;
}

interface ApprovalCardProps {
  approval: Approval;
  onApprove: (id: string) => void;
  onReject: (id: string) => void;
}

const riskColors = {
  low: "border-emerald-700 bg-emerald-900/20",
  medium: "border-amber-700 bg-amber-900/20",
  high: "border-red-700 bg-red-900/20",
  critical: "border-red-500 bg-red-900/40",
};

export function ApprovalCard({ approval, onApprove, onReject }: ApprovalCardProps) {
  const riskClass = riskColors[approval.risk_level as keyof typeof riskColors] || "border-zinc-700";

  return (
    <div className={`border rounded-lg p-4 ${riskClass}`}>
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm font-mono text-zinc-300">{approval.tool_name}</span>
        <span className={`text-xs px-2 py-0.5 rounded-full border ${
          approval.approval_status === "pending" ? "bg-amber-500/15 text-amber-300 border-amber-500/40" :
          approval.approval_status === "approved" ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/40" :
          "bg-rose-500/15 text-rose-300 border-rose-500/40"
        }`}>
          {approval.approval_status}
        </span>
      </div>
      <div className="text-sm text-zinc-100 mb-2">{approval.action_description}</div>
      <div className="flex items-center justify-between text-xs text-zinc-400">
        <span>{approval.cluster_id}</span>
        <span>{new Date(approval.created_at).toLocaleString()}</span>
      </div>
      {approval.approval_status === "pending" && (
        <div className="flex gap-2 mt-3">
          <button
            onClick={() => onApprove(approval.approval_id)}
            className="flex-1 py-2 bg-emerald-600 text-white text-sm rounded hover:bg-emerald-500 transition-colors"
          >
            승인
          </button>
          <button
            onClick={() => onReject(approval.approval_id)}
            className="flex-1 py-2 bg-red-600 text-white text-sm rounded hover:bg-red-500 transition-colors"
          >
            거부
          </button>
        </div>
      )}
    </div>
  );
}
