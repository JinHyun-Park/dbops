"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Search } from "lucide-react";
import { fetchClusters } from "@/lib/api-client";
import {
  normalizeClusters,
  setSelectedCluster,
  type ClusterLite,
} from "@/lib/selected-cluster";

interface Command {
  id: string;
  label: string;
  path: string;
  group: string;
  cluster?: string; // present when this row switches to a cluster
}

// Mirrors the sidebar taxonomy so the palette and the rail tell the same
// story. Labels keep the English page name + a short Korean gloss.
const commands: Command[] = [
  {
    id: "fleet",
    label: "Fleet — 전체 클러스터",
    path: "/fleet",
    group: "Monitor",
  },
  {
    id: "dashboard",
    label: "Dashboard — 단일 클러스터",
    path: "/dashboard",
    group: "Monitor",
  },
  {
    id: "compare",
    label: "Compare — 비교 분석",
    path: "/compare",
    group: "Monitor",
  },
  {
    id: "slo",
    label: "SLO — 가용성·지연 예산",
    path: "/slo",
    group: "Monitor",
  },
  {
    id: "schema",
    label: "Schema — FK 계보·의존성",
    path: "/schema",
    group: "Monitor",
  },

  { id: "chat", label: "Chat — AI 대화", path: "/chat", group: "Automate" },
  {
    id: "query-lab",
    label: "Query Lab — SQL 분석",
    path: "/query-lab",
    group: "Automate",
  },
  {
    id: "approvals",
    label: "Approvals — 승인 센터",
    path: "/approvals",
    group: "Automate",
  },
  {
    id: "ask",
    label: "Ask the fleet — 자연어 질의",
    path: "/ask",
    group: "Automate",
  },
  {
    id: "runbooks",
    label: "Runbooks — 진단·처방",
    path: "/runbooks",
    group: "Automate",
  },
  {
    id: "simulator",
    label: "Simulator — what-if 시뮬",
    path: "/simulator",
    group: "Automate",
  },

  {
    id: "timeline",
    label: "Timeline — 통합 인시던트 피드",
    path: "/timeline",
    group: "Incident",
  },
  {
    id: "activity",
    label: "Activity — 감사·회고 로그",
    path: "/activity",
    group: "Incident",
  },
  {
    id: "workload-diff",
    label: "Workload diff — 쿼리 변화",
    path: "/workload-diff",
    group: "Incident",
  },

  {
    id: "alerts",
    label: "Alerts — 규칙·구독자",
    path: "/alerts",
    group: "Configure",
  },
  {
    id: "clusters",
    label: "Clusters — 클러스터 관리",
    path: "/clusters",
    group: "Configure",
  },
  {
    id: "reports",
    label: "Reports — 예약 리포트",
    path: "/reports",
    group: "Configure",
  },
  {
    id: "cost",
    label: "Cost — Bedrock 비용",
    path: "/cost",
    group: "Configure",
  },
  {
    id: "preferences",
    label: "Memory — 에이전트 기억",
    path: "/preferences",
    group: "Configure",
  },
  {
    id: "health",
    label: "Health — 자체 모니터링",
    path: "/health",
    group: "Configure",
  },
];

export function CommandPalette() {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [clusters, setClusters] = useState<ClusterLite[]>([]);
  const [clustersLoaded, setClustersLoaded] = useState(false);
  const router = useRouter();

  const q = query.trim().toLowerCase();
  const pageMatches = commands.filter((c) => c.label.toLowerCase().includes(q));
  // Clusters only surface once you type — dumping a fleet of dozens on every
  // ⌘K would bury the page commands. Typing the id finds it (typeahead).
  const clusterMatches: Command[] = q
    ? clusters
        .filter((c) => c.cluster_id.toLowerCase().includes(q))
        .slice(0, 50)
        .map((c) => ({
          id: `cluster:${c.cluster_id}`,
          label: c.cluster_id,
          path: `/dashboard?cluster=${encodeURIComponent(c.cluster_id)}`,
          group: "Cluster",
          cluster: c.cluster_id,
        }))
    : [];
  const filtered = [...pageMatches, ...clusterMatches];

  const open = useCallback(() => {
    setIsOpen(true);
    setQuery("");
  }, []);

  // Lazy-load the cluster list the first time the palette opens BY ANY PATH
  // (⌘K toggles isOpen directly, the sidebar button fires the open event) so
  // ⌘K can double as a fleet-scale cluster switcher (typeahead, no dropdown).
  useEffect(() => {
    if (isOpen && !clustersLoaded) {
      setClustersLoaded(true);
      fetchClusters()
        .then((r: unknown) => setClusters(normalizeClusters(r)))
        .catch(() => {});
    }
  }, [isOpen, clustersLoaded]);

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "k") {
      e.preventDefault();
      setIsOpen((prev) => !prev);
      setQuery("");
    }
    if (e.key === "Escape") setIsOpen(false);
  }, []);

  useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    // The sidebar Search button (and anything else) can open the palette by
    // dispatching this event — keeps the trigger decoupled from this state.
    window.addEventListener("dbops:open-command-palette", open);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("dbops:open-command-palette", open);
    };
  }, [handleKeyDown, open]);

  const handleSelect = (cmd: Command) => {
    setIsOpen(false);
    setQuery("");
    // Switching cluster persists the choice (localStorage + event) so the
    // header chip and every cluster-scoped page pick it up.
    if (cmd.cluster) setSelectedCluster(cmd.cluster);
    router.push(cmd.path);
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[20vh]">
      <div
        className="fixed inset-0 bg-black/70 backdrop-blur-sm"
        onClick={() => setIsOpen(false)}
      />
      <div className="relative w-full max-w-lg bg-zinc-900 border border-zinc-700 rounded-lg shadow-2xl overflow-hidden">
        <div className="h-1 bg-gradient-to-r from-emerald-300 via-sky-300 to-fuchsia-400" />
        <div className="flex items-center gap-2.5 px-4 border-b border-zinc-700">
          <Search
            size={16}
            strokeWidth={2}
            className="text-zinc-500 flex-shrink-0"
          />
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="페이지 · 클러스터 검색..."
            className="w-full py-3 bg-transparent text-zinc-100 focus:outline-none placeholder:text-zinc-600"
            onKeyDown={(e) => {
              if (e.key === "Enter" && filtered.length > 0) {
                handleSelect(filtered[0]);
              }
            }}
          />
        </div>
        <div className="max-h-72 overflow-y-auto py-1">
          {filtered.map((cmd) => (
            <button
              key={cmd.id}
              onClick={() => handleSelect(cmd)}
              className="w-full flex items-center justify-between px-4 py-2 text-left hover:bg-zinc-800 transition-colors"
            >
              <span
                className={`text-sm ${
                  cmd.cluster ? "font-mono text-zinc-100" : "text-zinc-200"
                }`}
              >
                {cmd.label}
              </span>
              <span
                className={`text-[10px] uppercase tracking-wider ${
                  cmd.cluster ? "text-sky-300/80" : "text-emerald-300/70"
                }`}
              >
                {cmd.group}
              </span>
            </button>
          ))}
          {filtered.length === 0 && (
            <div className="px-4 py-8 text-center text-zinc-500 text-sm">
              결과 없음
            </div>
          )}
        </div>
        <div className="px-4 py-2 border-t border-zinc-700 text-xs text-zinc-500">
          ⌘K로 열기 · Enter로 이동 · Esc로 닫기
        </div>
      </div>
    </div>
  );
}
