"use client";

import { useEffect, useState } from "react";
import { fetchExtensions, type InstalledExtension, type RecommendedExtension } from "@/lib/api-client";

export function ExtensionsCard({ clusterId, engine }: { clusterId: string; engine?: string }) {
  const [installed, setInstalled] = useState<InstalledExtension[]>([]);
  const [recommended, setRecommended] = useState<RecommendedExtension[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAll, setShowAll] = useState(false);

  // Currently PG-only — MySQL has no pg_extension equivalent.
  const isPg = (engine || "").includes("postgresql");

  useEffect(() => {
    if (!isPg) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    fetchExtensions(clusterId)
      .then((d) => {
        if (cancelled) return;
        setInstalled(d.installed || []);
        setRecommended(d.recommended || []);
      })
      .catch(() => !cancelled && setInstalled([]))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [clusterId, isPg]);

  if (!isPg) {
    return (
      <div className="bg-zinc-900/50 border border-zinc-800 p-4">
        <div className="text-xs text-zinc-400 uppercase tracking-wider mb-2">Extensions</div>
        <div className="text-xs text-zinc-500">PostgreSQL-only.</div>
      </div>
    );
  }

  const otherInstalled = installed.filter(
    (e) => !recommended.some((r) => r.extname === e.extname),
  );

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <div className="text-xs text-zinc-400 uppercase tracking-wider">
            Recommended Extensions
            <span className="ml-2 text-[10px] text-zinc-600">
              {recommended.filter((r) => r.installed).length} / {recommended.length} installed
            </span>
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            DBOps가 PG 클러스터에 권장하는 모듈 — 미설치 항목은 hover로 이유 확인.
          </div>
        </div>
      </div>

      {loading ? (
        <div className="text-zinc-500 text-sm">Loading…</div>
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
            {recommended.map((r) => (
              <div
                key={r.extname}
                className={`flex items-center gap-2 px-2.5 py-1.5 border ${
                  r.installed
                    ? "border-emerald-500/30 bg-emerald-500/5"
                    : r.severity === "warning"
                    ? "border-amber-500/30 bg-amber-500/5"
                    : "border-zinc-800 bg-zinc-900/30"
                }`}
                title={r.why}
              >
                <span
                  className={`text-xs ${
                    r.installed ? "text-emerald-400" : r.severity === "warning" ? "text-amber-400" : "text-zinc-500"
                  }`}
                >
                  {r.installed ? "✓" : "✗"}
                </span>
                <span className="text-xs font-mono text-zinc-200 flex-1 truncate">{r.extname}</span>
                {!r.installed && (
                  <span
                    className={`text-[9px] uppercase tracking-wider px-1 py-0.5 ${
                      r.severity === "warning"
                        ? "border border-amber-500/40 text-amber-300"
                        : "border border-zinc-700 text-zinc-500"
                    }`}
                  >
                    {r.severity}
                  </span>
                )}
              </div>
            ))}
          </div>

          {otherInstalled.length > 0 && (
            <div className="mt-3 pt-3 border-t border-zinc-800">
              <button
                onClick={() => setShowAll((v) => !v)}
                className="text-[10px] uppercase tracking-wider text-zinc-500 hover:text-zinc-300 transition-colors"
              >
                other installed extensions ({otherInstalled.length}) {showAll ? "▾" : "▸"}
              </button>
              {showAll && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {otherInstalled.map((e) => (
                    <span
                      key={e.extname}
                      className="text-[10px] font-mono px-1.5 py-0.5 border border-zinc-800 bg-zinc-900/50 text-zinc-400"
                      title={`v${e.extversion}`}
                    >
                      {e.extname}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
