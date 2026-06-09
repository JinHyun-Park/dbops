"use client";

import Link from "next/link";
import { rcaChatHref } from "@/lib/rca-link";

// "AI 근본원인 분석" entry point. Routes to the AI chat with a cluster-scoped
// RCA prompt pre-filled (one keystroke to run) — the agent does the actual
// diagnosis via its diagnose_root_cause / correlate_signals MCP tools.
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
  const base =
    variant === "prominent"
      ? "bg-amber-500 text-zinc-950 hover:bg-amber-400 border border-amber-500"
      : "border border-zinc-800 text-zinc-400 hover:border-amber-500/60 hover:text-amber-200";
  return (
    <Link
      href={rcaChatHref(clusterId)}
      className={`inline-flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 transition-colors ${base} ${className}`}
      title="AI 에이전트가 최근 신호를 상관분석해 근본 원인 후보를 정리합니다"
    >
      🔍 {label}
    </Link>
  );
}
