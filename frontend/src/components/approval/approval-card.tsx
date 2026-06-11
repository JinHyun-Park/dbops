"use client";

import { useState } from "react";

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
  // Snapshot creation is non-destructive (adds a backup) → low risk.
  create_snapshot: "low",
  // Restore stands up a NEW billable cluster → high risk (source untouched).
  restore_cluster: "high",
  // Data API 활성화: 데이터 변경은 없지만 SQL 실행 경로가 IAM 경계로
  // 열리는 설정 변경 — 중간 위험으로 표시해 DBA가 의미를 인지하고 승인.
  enable_data_api: "medium",
  other: "medium",
};

// action_type별 "이 작업이 무엇이고, 무슨 리스크가 있고, 승인 전 무엇을
// 점검해야 하는지" 가이드. 승인 카드만 보고는 요청의 의미·위험을 알기
// 어려워(파라미터 값만 보임) DBA가 매번 따로 판단해야 했다 — 결정에 필요한
// 컨텍스트를 카드 안에서 바로 제공한다.
interface ActionGuide {
  what: string;
  risks: string[];
  considerations: string[];
}

const ACTION_GUIDE: Record<string, ActionGuide> = {
  execute_sql: {
    what: "임의 SQL(DDL/DML)을 대상 클러스터에서 직접 실행합니다.",
    risks: [
      "락 경합 — DDL은 테이블 잠금을 유발해 운영 쿼리를 막을 수 있습니다.",
      "롤백 난이도 — DML은 트랜잭션이지만 DDL은 대부분 즉시 확정됩니다.",
      "force=true면 DROP/TRUNCATE/DELETE 같은 파괴적 구문입니다.",
    ],
    considerations: [
      "실행 SQL이 의도한 스키마/테이블만 건드리는지 확인",
      "트래픽이 적은 시간대(유지보수 윈도우) 권장",
      "대형 테이블이면 CONCURRENTLY/배치 분할 검토",
    ],
  },
  modify_parameter: {
    what: "DB 파라미터 그룹의 설정값을 변경합니다.",
    risks: [
      "static 파라미터는 적용에 인스턴스 재시작이 필요 — 짧은 다운타임 발생.",
      "메모리 계열(work_mem 등)은 max_connections와 곱해져 OOM을 유발할 수 있습니다.",
      "플래너 파라미터는 쿼리 플랜을 바꿔 성능 회귀 가능.",
    ],
    considerations: [
      "static/dynamic 여부 확인 — static이면 재시작 타이밍 계획",
      "Simulator의 파라미터 영향 추정으로 사전 검토",
      "변경 후 변경 영향 회고 패널로 전후 비교",
    ],
  },
  modify_scaling: {
    what: "Serverless v2 ACU 범위를 변경합니다.",
    risks: [
      "min ACU를 너무 낮추면 콜드스타트 지연, max를 낮추면 스파이크 시 throttle.",
      "프로비저닝 클러스터에는 적용되지 않습니다(인스턴스 클래스 변경 필요).",
    ],
    considerations: [
      "관측된 평균/피크 ACU 대비 적정 범위인지 확인",
      "비용 영향은 Cost 탭·Simulator로 추정",
    ],
  },
  manage_maintenance: {
    what: "유지보수 윈도우를 조회하거나 변경합니다.",
    risks: ["윈도우를 트래픽 피크 시간으로 옮기면 자동 패치가 운영에 영향."],
    considerations: ["저트래픽 시간대로 설정", "백업 윈도우와 겹치지 않게"],
  },
  create_snapshot: {
    what: "수동 클러스터 스냅샷(백업)을 생성합니다.",
    risks: ["비파괴적 — 데이터 변경 없음. 스냅샷 스토리지 비용만 발생."],
    considerations: ["대용량이면 생성에 시간 소요", "보존 정책 확인"],
  },
  restore_cluster: {
    what: "스냅샷 또는 특정 시점(PITR)을 새 클러스터로 복원합니다.",
    risks: [
      "원본 클러스터는 영향 없음 — 새 클러스터가 생성됩니다.",
      "새 클러스터는 과금 대상 — 사용 후 정리 필요.",
      "복원에 수십 분 소요될 수 있습니다.",
    ],
    considerations: [
      "복원 대상 시점/스냅샷이 정확한지 확인",
      "새 클러스터 ID·엔드포인트 전환 계획",
    ],
  },
  enable_data_api: {
    what: "RDS Data API(HttpEndpoint)를 활성화합니다.",
    risks: [
      "데이터 변경은 없으나 SQL 실행 경로가 네트워크 경계에서 IAM 경계로 열립니다.",
      "rds-data:ExecuteStatement + 시크릿 권한이 있으면 어디서든 쿼리 가능.",
    ],
    considerations: [
      "다운타임 없음 — 설정 변경만",
      "활성화 후 라이브 SQL 수집·에이전트 SQL이 동작",
    ],
  },
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
  const guide = ACTION_GUIDE[action];
  const [showGuide, setShowGuide] = useState(false);

  return (
    <div className={`border p-4 ${riskClass}`}>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-mono text-zinc-300">{action}</span>
          <span
            className={`text-[9px] uppercase tracking-wider px-1.5 py-0.5 border ${riskClass}`}
            title="이 작업의 위험도"
          >
            {risk}
          </span>
        </div>
        <StatusPill status={approval.approval_status} />
      </div>

      {guide && (
        <div className="text-[11px] text-zinc-400 mb-2">{guide.what}</div>
      )}

      {/* Per-action_type detail renderer */}
      <ActionDetails action={action} details={details} />

      {/* 리스크·고려사항 — 승인 결정에 필요한 컨텍스트를 카드 안에서 바로
          제공한다. 기본 접힘(공간 절약), 클릭 시 펼침. */}
      {guide && (guide.risks.length > 0 || guide.considerations.length > 0) && (
        <div className="mt-2">
          <button
            onClick={() => setShowGuide((v) => !v)}
            className="text-[11px] text-sky-400 hover:text-sky-300"
          >
            {showGuide ? "▾" : "▸"} 리스크·고려사항
          </button>
          {showGuide && (
            <div className="mt-2 space-y-2 border-l-2 border-zinc-700 pl-3">
              {guide.risks.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-rose-300/80 mb-1">
                    리스크
                  </div>
                  <ul className="text-[11px] text-zinc-300 space-y-0.5 list-disc list-inside">
                    {guide.risks.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                </div>
              )}
              {guide.considerations.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-amber-300/80 mb-1">
                    승인 전 점검
                  </div>
                  <ul className="text-[11px] text-zinc-300 space-y-0.5 list-disc list-inside">
                    {guide.considerations.map((c, i) => (
                      <li key={i}>{c}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      <div className="flex items-center justify-between text-[11px] text-zinc-500 mt-3 pt-3 border-t border-zinc-800/80">
        <span className="font-mono">{approval.cluster_id}</span>
        {/* created_at is a ms-epoch stored as a DDB STRING (sort key) —
            new Date("1781…") is Invalid Date, so cast numerics first. */}
        <span>
          {new Date(
            Number.isFinite(Number(approval.created_at))
              ? Number(approval.created_at)
              : approval.created_at,
          ).toLocaleString()}
        </span>
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

  if (action === "restore_cluster") {
    const mode = String(details.mode ?? "snapshot");
    return (
      <div className="text-sm text-zinc-100 space-y-1.5">
        <DetailRow
          label="new cluster"
          value={String(details.new_cluster_id ?? "")}
          mono
        />
        <DetailRow label="mode" value={mode} />
        {mode === "pitr" ? (
          <DetailRow
            label="restore to"
            value={
              details.use_latest === true
                ? "latest restorable time"
                : String(details.restore_to_time ?? "")
            }
            mono
          />
        ) : (
          <DetailRow
            label="snapshot"
            value={String(details.snapshot_id ?? "")}
            mono
          />
        )}
        <div className="text-[11px] text-amber-300/90">
          ⚠ 새 클러스터를 생성합니다 (과금 발생). 소스 클러스터는 변경되지
          않습니다.
        </div>
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
