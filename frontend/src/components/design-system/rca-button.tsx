"use client";

import { useRca } from "@/components/rca/rca-drawer";

// "AI 근본원인 분석" entry point. Opens the in-place RCA side panel (streams the
// agent's diagnose_root_cause analysis without leaving the page and without
// touching the user's chat history). Falls back to a no-op if rendered outside
// the RcaProvider.
export function RcaButton({
  clusterId,
  className = "",
  label = "AI 근본원인 분석",
  variant = "default",
}: {
  clusterId: string | null | undefined;
  className?: string;
  label?: string;
  variant?: "default" | "prominent";
}) {
  const { open } = useRca();
  const base =
    variant === "prominent"
      ? "bg-amber-500 text-zinc-950 hover:bg-amber-400 border border-amber-500"
      : "border border-zinc-800 text-zinc-400 hover:border-amber-500/60 hover:text-amber-200";
  return (
    <button
      type="button"
      disabled={!clusterId}
      onClick={() => clusterId && open(clusterId)}
      className={`inline-flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 transition-colors disabled:opacity-50 ${base} ${className}`}
      title="AI 에이전트가 최근 신호를 상관분석해 근본 원인 후보를 정리합니다"
    >
      🔍 {label}
    </button>
  );
}
