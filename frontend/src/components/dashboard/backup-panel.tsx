"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchBackups,
  createSnapshot,
  type BackupsResponse,
  type BackupSnapshot,
} from "@/lib/api-client";
import { fmtNumber } from "@/lib/format";
import { isAdmin } from "@/lib/auth";

function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  return `${Math.floor(h / 24)}일 전`;
}

export function BackupPanel({ clusterId }: { clusterId: string }) {
  const [data, setData] = useState<BackupsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [showSnapshots, setShowSnapshots] = useState(false);
  // Manual snapshot creation (phase 2). Admin-only; the modal lets the
  // DBA optionally name the snapshot or accept an auto-generated id.
  const [admin, setAdmin] = useState(false);
  const [snapOpen, setSnapOpen] = useState(false);
  const [snapName, setSnapName] = useState("");
  const [creating, setCreating] = useState(false);
  const [snapError, setSnapError] = useState<string | null>(null);
  const [snapToast, setSnapToast] = useState<string | null>(null);

  useEffect(() => {
    setAdmin(isAdmin());
  }, []);

  const load = useCallback(() => {
    setLoading(true);
    fetchBackups(clusterId)
      .then(setData)
      .catch((e) =>
        setData({
          cluster_id: clusterId,
          engine: "",
          status: "",
          error: e instanceof Error ? e.message : String(e),
          backup_retention_days: null,
          preferred_backup_window: null,
          earliest_restorable_time: null,
          latest_restorable_time: null,
          pitr_window_hours: null,
          snapshot_count: 0,
          manual_snapshot_count: 0,
          snapshots: [],
          checked_at: Date.now(),
        }),
      )
      .finally(() => setLoading(false));
  }, [clusterId]);

  useEffect(() => {
    load();
  }, [load]);

  const submitSnapshot = useCallback(async () => {
    setCreating(true);
    setSnapError(null);
    try {
      const r = await createSnapshot(clusterId, snapName.trim() || undefined);
      setSnapToast(r.message || `스냅샷 ${r.snapshot_id} 생성 시작`);
      setSnapOpen(false);
      setSnapName("");
      // Snapshot shows as "creating" immediately; reload picks it up.
      load();
    } catch (e) {
      setSnapError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  }, [clusterId, snapName, load]);

  const retention = Number(data?.backup_retention_days || 0);
  const retentionColor =
    retention < 1
      ? "text-rose-400"
      : retention < 7
        ? "text-amber-400"
        : "text-emerald-400";

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="text-sm text-zinc-200 font-medium">
          Backup & Recovery
          {data && !data.error && (
            <span className="ml-2 px-1.5 py-0.5 bg-sky-500/15 text-sky-300 border border-sky-500/30 text-[10px]">
              {data.snapshot_count} snapshots
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {admin && (
            <button
              onClick={() => {
                setSnapError(null);
                setSnapOpen(true);
              }}
              className="text-[10px] px-2 py-1 border border-zinc-700 text-zinc-300 hover:border-amber-500/60 hover:text-amber-200 transition-colors"
            >
              + 스냅샷 생성
            </button>
          )}
          <button
            onClick={load}
            disabled={loading}
            className="text-[10px] text-zinc-500 hover:text-zinc-300 disabled:opacity-50"
          >
            {loading ? "…" : "↻"}
          </button>
        </div>
      </div>

      {snapToast && (
        <div className="mb-3 px-3 py-2 border border-emerald-500/40 bg-emerald-500/10 text-emerald-200 text-xs flex items-start justify-between gap-3">
          <span>{snapToast}</span>
          <button
            onClick={() => setSnapToast(null)}
            className="text-emerald-300/70 hover:text-emerald-200 flex-shrink-0"
          >
            ✕
          </button>
        </div>
      )}

      {/* Inline create-snapshot form — admin-only, opened by the button */}
      {snapOpen && (
        <div className="mb-4 border border-zinc-800 bg-zinc-950 p-3">
          <div className="text-[11px] text-zinc-400 mb-2">
            수동 스냅샷을 생성합니다. 이름을 비워두면 자동으로 생성됩니다 (예:
            manual-…-타임스탬프). 스냅샷 생성은 데이터를 변경하지 않는 안전한
            작업입니다.
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={snapName}
              onChange={(e) => setSnapName(e.target.value)}
              placeholder="snapshot id (선택)"
              className="flex-1 min-w-[180px] bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 font-mono focus:outline-none focus:border-amber-500/60"
            />
            <button
              onClick={submitSnapshot}
              disabled={creating}
              className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors"
            >
              {creating ? "생성 중…" : "생성"}
            </button>
            <button
              onClick={() => {
                setSnapOpen(false);
                setSnapError(null);
              }}
              className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
            >
              취소
            </button>
          </div>
          {snapError && (
            <div className="text-[11px] text-rose-300 mt-2">{snapError}</div>
          )}
        </div>
      )}

      {data?.error && (
        <div className="text-xs text-rose-300 mb-3">{data.error}</div>
      )}

      {/* Summary grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <div>
          <div className="text-zinc-500 text-xs mb-1">Retention</div>
          <div className={`font-mono ${retentionColor}`}>
            {retention > 0 ? `${retention}d` : "—"}
          </div>
        </div>
        <div>
          <div className="text-zinc-500 text-xs mb-1">PITR Window</div>
          <div className="text-zinc-100 font-mono">
            {data?.pitr_window_hours != null
              ? data.pitr_window_hours < 24
                ? `${data.pitr_window_hours}h`
                : `${(data.pitr_window_hours / 24).toFixed(1)}d`
              : "—"}
          </div>
        </div>
        <div>
          <div className="text-zinc-500 text-xs mb-1">Latest Restore</div>
          <div className="text-zinc-300 text-xs">
            {relTime(data?.latest_restorable_time)}
          </div>
        </div>
        <div>
          <div className="text-zinc-500 text-xs mb-1">Backup Window</div>
          <div className="text-zinc-300 font-mono text-xs">
            {data?.preferred_backup_window || "—"}
          </div>
        </div>
      </div>

      {/* PITR visual bar */}
      {data?.earliest_restorable_time && data?.latest_restorable_time && (
        <div className="mt-4">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5">
            Point-in-Time Recovery 윈도우
          </div>
          <div className="relative h-5 bg-zinc-800 border border-zinc-700 overflow-hidden">
            <div
              className="absolute inset-y-0 left-0 bg-emerald-500/20 border-r border-emerald-500/40"
              style={{ width: "100%" }}
            />
            <div className="absolute inset-0 flex items-center justify-between px-2 text-[10px] font-mono text-zinc-400">
              <span>
                {new Date(data.earliest_restorable_time).toLocaleString()}
              </span>
              <span className="text-emerald-300">
                {new Date(data.latest_restorable_time).toLocaleString()}
              </span>
            </div>
          </div>
          <div className="text-[10px] text-zinc-600 mt-0.5">
            이 구간의 아무 시점으로 복원할 수 있습니다 (PITR)
          </div>
        </div>
      )}

      {/* Snapshot inventory — collapsed by default */}
      {data && data.snapshots.length > 0 && (
        <div className="mt-4">
          <button
            onClick={() => setShowSnapshots(!showSnapshots)}
            className="text-[10px] uppercase tracking-wider text-zinc-500 hover:text-zinc-300 transition-colors"
          >
            {showSnapshots ? "▾" : "▸"} Snapshots ({data.manual_snapshot_count}{" "}
            manual / {data.snapshot_count - data.manual_snapshot_count}{" "}
            automated)
          </button>
          {showSnapshots && (
            <div className="mt-2 border border-zinc-800 divide-y divide-zinc-800 max-h-64 overflow-y-auto">
              {data.snapshots.map((s) => (
                <SnapshotRow key={s.id} snapshot={s} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SnapshotRow({ snapshot: s }: { snapshot: BackupSnapshot }) {
  const isManual = s.type === "manual";
  return (
    <div className="px-3 py-2 flex items-baseline justify-between gap-3">
      <div className="flex items-center gap-2 min-w-0">
        <span
          className={`text-[10px] font-mono px-1 py-0.5 border ${
            isManual
              ? "bg-amber-500/10 text-amber-300 border-amber-500/40"
              : "bg-zinc-700/40 text-zinc-400 border-zinc-700"
          }`}
        >
          {isManual ? "manual" : "auto"}
        </span>
        <span className="text-xs text-zinc-200 font-mono truncate">{s.id}</span>
      </div>
      <div className="text-[10px] text-zinc-500 tabular-nums flex-shrink-0">
        {s.created ? relTime(s.created) : "—"}
        {s.allocated_storage_gb != null && (
          <span className="ml-2">{fmtNumber(s.allocated_storage_gb)} GB</span>
        )}
      </div>
    </div>
  );
}
