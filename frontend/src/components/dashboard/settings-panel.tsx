"use client";

import { useEffect, useState } from "react";
import { fetchClusterSettings } from "@/lib/api-client";

interface Setting {
  name: string;
  value: string;
  unit: string;
  updated_at: string;
}

function fmtValue(s: Setting): string {
  const v = s.value;
  const u = s.unit;
  if (!u) return v;
  const num = Number(v);
  if (!Number.isFinite(num)) return `${v} ${u}`;
  // unit conversions
  if (u === "8kB") return `${((num * 8) / 1024).toFixed(1)} MB`;
  if (u === "kB") return `${(num / 1024).toFixed(1)} MB`;
  if (u === "s") return `${num}s`;
  if (u === "ms") return `${num}ms`;
  return `${v} ${u}`;
}

export function SettingsPanel({ clusterId }: { clusterId: string }) {
  const [settings, setSettings] = useState<Setting[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchClusterSettings(clusterId)
      .then((d) => !cancelled && setSettings(d.settings || []))
      .catch(() => !cancelled && setSettings([]))
      .finally(() => !cancelled && setLoading(false));
  }, [clusterId]);

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">
        PostgreSQL Configuration
      </div>
      {loading ? (
        <div className="text-zinc-500 text-sm">Loading...</div>
      ) : settings.length === 0 ? (
        <div className="text-zinc-500 text-sm">no settings collected</div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          {settings.map((s) => (
            <div key={s.name} className="bg-zinc-950 border border-zinc-800 rounded p-3">
              <div className="text-[11px] text-zinc-500 font-mono mb-1 truncate" title={s.name}>
                {s.name}
              </div>
              <div className="text-sm text-zinc-100 font-mono">{fmtValue(s)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
