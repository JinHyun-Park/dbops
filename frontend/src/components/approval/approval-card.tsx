"use client";

// Approval rows arrive from two slightly different writers:
//   1. POST /api/approvals (old path) — sets `tool_name`,
//      `action_description`, `risk_level`, `parameters` (JSON string).
//   2. mcp-servers request_approval tool (new path) — sets
//      `action_type` + `action_details` (object), no risk_level.
// Render handles both shapes so a single page can mix legacy + new
// approvals while migration is in flight.
interface Approval {
  approval_id: string;
  cluster_id: string;
  approval_status: string;
  requested_by: string;
  created_at: string;

  // Legacy shape
  tool_name?: string;
  action_description?: string;
  parameters?: string;
  risk_level?: string;

  // New shape
  action_type?: string;
  action_details?: Record<string, unknown> | string;
}

interface ApprovalCardProps {
  approval: Approval;
  onApprove: (id: string) => void;
  onReject: (id: string) => void;
}

const riskColors: Record<string, string> = {
  low: "border-emerald-700 bg-emerald-900/20",
  medium: "border-amber-700 bg-amber-900/20",
  high: "border-red-700 bg-red-900/20",
  critical: "border-red-500 bg-red-900/40",
};

// Map action_type → implicit risk so the new shape gets a colored
// border without the writer having to set risk_level.
const ACTION_RISK: Record<string, string> = {
  execute_sql: "high",
  modify_parameter: "medium",
  modify_scaling: "medium",
  manage_maintenance: "low",
  other: "medium",
};

export function ApprovalCard({
  approval,
  onApprove,
  onReject,
}: ApprovalCardProps) {
  const action = approval.action_type || approval.tool_name || "unknown";
  const risk =
    approval.risk_level || ACTION_RISK[approval.action_type || ""] || "medium";
  const riskClass = riskColors[risk] || "border-zinc-700";
  const details = parseDetails(approval);

  return (
    <div className={`border p-4 ${riskClass}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-mono text-zinc-300">{action}</span>
        <StatusPill status={approval.approval_status} />
      </div>

      {/* Per-action_type detail renderer */}
      <ActionDetails action={action} details={details} />

      <div className="flex items-center justify-between text-[11px] text-zinc-500 mt-3 pt-3 border-t border-zinc-800/80">
        <span className="font-mono">{approval.cluster_id}</span>
        <span>{new Date(approval.created_at).toLocaleString()}</span>
      </div>

      {/* approval_id is what the agent needs when re-issuing the write
          tool — DBA can copy it into chat if the agent forgot to surface
          it. */}
      <div className="text-[10px] font-mono text-zinc-600 mt-1 truncate">
        id: {approval.approval_id}
      </div>

      {approval.approval_status === "pending" && (
        <div className="flex gap-2 mt-3">
          <button
            onClick={() => onApprove(approval.approval_id)}
            className="flex-1 py-2 bg-emerald-600 text-white text-sm hover:bg-emerald-500 transition-colors"
          >
            승인
          </button>
          <button
            onClick={() => onReject(approval.approval_id)}
            className="flex-1 py-2 bg-red-600 text-white text-sm hover:bg-red-500 transition-colors"
          >
            거부
          </button>
        </div>
      )}
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const cls =
    status === "pending"
      ? "bg-amber-500/15 text-amber-300 border-amber-500/40"
      : status === "approved"
        ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/40"
        : status === "consumed"
          ? "bg-zinc-700 text-zinc-300 border-zinc-600"
          : "bg-rose-500/15 text-rose-300 border-rose-500/40";
  return <span className={`text-xs px-2 py-0.5 border ${cls}`}>{status}</span>;
}

function parseDetails(approval: Approval): Record<string, unknown> {
  const raw = approval.action_details ?? approval.parameters;
  if (!raw) return {};
  if (typeof raw === "string") {
    try {
      return JSON.parse(raw);
    } catch {
      return { _raw: raw };
    }
  }
  return raw as Record<string, unknown>;
}

function ActionDetails({
  action,
  details,
}: {
  action: string;
  details: Record<string, unknown>;
}) {
  // Legacy free-text description (no action_details).
  if (!details || Object.keys(details).length === 0) {
    return null;
  }

  if (action === "execute_sql") {
    const sql = String(details.sql ?? "");
    return (
      <div className="space-y-2">
        <pre className="bg-zinc-950 border border-zinc-800 p-3 text-xs text-zinc-200 font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
          {sql || "(no SQL provided)"}
        </pre>
        {details.force === true && (
          <div className="text-[11px] text-rose-300">
            ⚠ force=true — DROP/TRUNCATE/DELETE class statement
          </div>
        )}
      </div>
    );
  }

  if (action === "modify_parameter") {
    return (
      <div className="text-sm text-zinc-100 space-y-1.5">
        <DetailRow
          label="parameter"
          value={String(details.parameter_name ?? "")}
          mono
        />
        <DetailRow label="new value" value={String(details.value ?? "")} mono />
      </div>
    );
  }

  if (action === "modify_scaling") {
    return (
      <div className="text-sm text-zinc-100 space-y-1.5">
        <DetailRow label="min ACU" value={fmt(details.min_capacity)} mono />
        <DetailRow label="max ACU" value={fmt(details.max_capacity)} mono />
      </div>
    );
  }

  if (action === "manage_maintenance") {
    return (
      <div className="text-sm text-zinc-100 space-y-1.5">
        <DetailRow label="action" value={String(details.action ?? "")} />
        <DetailRow label="window" value={String(details.window ?? "")} mono />
      </div>
    );
  }

  // Fallback: pretty-print whatever JSON was sent.
  return (
    <pre className="bg-zinc-950 border border-zinc-800 p-3 text-[11px] text-zinc-300 font-mono whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
      {JSON.stringify(details, null, 2)}
    </pre>
  );
}

function DetailRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-3">
      <span className="text-[11px] uppercase tracking-wider text-zinc-500 w-24 flex-shrink-0">
        {label}
      </span>
      <span
        className={`text-sm text-zinc-100 break-all ${mono ? "font-mono" : ""}`}
      >
        {value || "—"}
      </span>
    </div>
  );
}

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}
