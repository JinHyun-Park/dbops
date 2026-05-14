"use client";

import { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";

const commands = [
  { id: "chat", label: "Chat — AI 대화", path: "/chat", shortcut: "C" },
  {
    id: "dashboard",
    label: "Dashboard — 모니터링",
    path: "/dashboard",
    shortcut: "D",
  },
  {
    id: "query-lab",
    label: "Query Lab — SQL 분석",
    path: "/query-lab",
    shortcut: "Q",
  },
  { id: "reports", label: "Reports — 리포트", path: "/reports", shortcut: "R" },
  {
    id: "approvals",
    label: "Approvals — 승인 센터",
    path: "/approvals",
    shortcut: "A",
  },
  {
    id: "clusters",
    label: "Clusters — 클러스터 관리",
    path: "/clusters",
    shortcut: "L",
  },
];

export function CommandPalette() {
  const [isOpen, setIsOpen] = useState(false);
  const [query, setQuery] = useState("");
  const router = useRouter();

  const filtered = commands.filter((c) =>
    c.label.toLowerCase().includes(query.toLowerCase()),
  );

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
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  const handleSelect = (path: string) => {
    setIsOpen(false);
    setQuery("");
    router.push(path);
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[20vh]">
      <div
        className="fixed inset-0 bg-black/60"
        onClick={() => setIsOpen(false)}
      />
      <div className="relative w-full max-w-lg bg-zinc-900 border border-zinc-700 rounded-xl shadow-2xl overflow-hidden">
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="명령어 검색..."
          className="w-full px-4 py-3 bg-transparent text-zinc-100 border-b border-zinc-700 focus:outline-none"
          onKeyDown={(e) => {
            if (e.key === "Enter" && filtered.length > 0) {
              handleSelect(filtered[0].path);
            }
          }}
        />
        <div className="max-h-64 overflow-y-auto">
          {filtered.map((cmd) => (
            <button
              key={cmd.id}
              onClick={() => handleSelect(cmd.path)}
              className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-zinc-800 transition-colors"
            >
              <span className="text-sm text-zinc-200">{cmd.label}</span>
              <kbd className="text-xs text-zinc-500 bg-zinc-800 px-2 py-0.5 rounded border border-zinc-700">
                {cmd.shortcut}
              </kbd>
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
