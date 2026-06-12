"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchBackups,
  createSnapshot,
  restoreCluster,
  type BackupsResponse,
  type BackupSnapshot,
} from "@/lib/api-client";
import { fmtNumber } from "@/lib/format";
import { isAdmin } from "@/lib/auth";
import { engineFamily } from "@/lib/engine";

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

export function BackupPanel({
  clusterId,
  engine,
}: {
  clusterId: string;
  engine?: string;
}) {
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
  // Restore (phase 3). The strongest write in the backup workflow — it
  // stands up a NEW billable cluster. restoreMode !== null opens the form;
  // type-to-confirm (re-typing the new cluster id) gates the submit.
  const [restoreMode, setRestoreMode] = useState<"snapshot" | "pitr" | null>(
    null,
  );
  const [restoreSnapId, setRestoreSnapId] = useState("");
  const [restoreToTime, setRestoreToTime] = useState("");
  const [useLatest, setUseLatest] = useState(true);
  const [newClusterId, setNewClusterId] = useState("");
  const [confirmId, setConfirmId] = useState("");
  const [restoring, setRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);

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

  const openRestore = useCallback(
    (mode: "snapshot" | "pitr", snapId = "") => {
      setRestoreMode(mode);
      setRestoreSnapId(snapId);
      setRestoreError(null);
      setNewClusterId(`${clusterId}-restore`.slice(0, 63));
      setConfirmId("");
      setUseLatest(true);
      setRestoreToTime("");
    },
    [clusterId],
  );

  const submitRestore = useCallback(async () => {
    if (!restoreMode) return;
    setRestoring(true);
    setRestoreError(null);
    try {
      const r = await restoreCluster(clusterId, {
        newClusterId: newClusterId.trim(),
        confirm: confirmId.trim(),
        mode: restoreMode,
        snapshotId: restoreMode === "snapshot" ? restoreSnapId : undefined,
        restoreToTime:
          restoreMode === "pitr" && !useLatest ? restoreToTime : undefined,
        useLatest: restoreMode === "pitr" ? useLatest : undefined,
      });
      setSnapToast(r.message || `복원 시작: ${r.new_cluster_id}`);
      setRestoreMode(null);
      setNewClusterId("");
      setConfirmId("");
      setRestoreSnapId("");
      load();
    } catch (e) {
      setRestoreError(e instanceof Error ? e.message : String(e));
    } finally {
      setRestoring(false);
    }
  }, [
    clusterId,
    restoreMode,
    newClusterId,
    confirmId,
    restoreSnapId,
    restoreToTime,
    useLatest,
    load,
  ]);

  const confirmMatches =
    newClusterId.trim().length > 0 && confirmId.trim() === newClusterId.trim();

  const retention = Number(data?.backup_retention_days || 0);
  const retentionColor =
    retention < 1
      ? "text-rose-400"
      : retention < 7
        ? "text-amber-400"
        : "text-emerald-400";

  const fam = engineFamily(engine);
  // Non-relational backup views are READ-ONLY — snapshot create / restore POST
  // to the Aurora-only write handler, so those controls are hidden here.
  const readOnly = fam !== "relational";

  // DynamoDB has no RDS-style cluster snapshots — show PITR posture + on-demand
  // backups instead. Enabling PITR / creating backups is an AWS Console/CDK
  // action (not yet in the platform's write surface).
  if (fam === "dynamodb") {
    const pitr = !!data?.pitr_enabled;
    const backups = data?.on_demand_backups ?? [];
    return (
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        <div className="flex items-center justify-between mb-3">
          <div className="text-sm text-zinc-200 font-medium">
            Backup &amp; Recovery
            <span className="ml-2 px-1.5 py-0.5 bg-zinc-700/40 text-zinc-400 border border-zinc-700 text-[10px]">
              읽기 전용
            </span>
          </div>
          <button
            onClick={load}
            disabled={loading}
            className="text-[10px] text-zinc-500 hover:text-zinc-300 disabled:opacity-50"
          >
            {loading ? "…" : "↻"}
          </button>
        </div>

        {data?.error && (
          <div
            className={`text-xs mb-3 px-3 py-2 border ${
              data.info
                ? "text-zinc-400 border-zinc-700 bg-zinc-800/30"
                : "text-rose-300 border-rose-500/40 bg-rose-500/10"
            }`}
          >
            {data.error}
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
          <div>
            <div className="text-zinc-500 text-xs mb-1">
              Point-in-Time Recovery
            </div>
            <div
              className={`font-mono ${
                pitr ? "text-emerald-400" : "text-zinc-500"
              }`}
            >
              {pitr ? "활성" : "비활성"}
            </div>
          </div>
          <div>
            <div className="text-zinc-500 text-xs mb-1">On-Demand Backups</div>
            <div className="text-zinc-100 font-mono">{backups.length}</div>
          </div>
          <div>
            <div className="text-zinc-500 text-xs mb-1">Latest Restorable</div>
            <div className="text-zinc-300 text-xs">
              {relTime(data?.latest_restorable_time)}
            </div>
          </div>
        </div>

        {pitr &&
          data?.earliest_restorable_time &&
          data?.latest_restorable_time && (
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
            </div>
          )}

        {backups.length > 0 ? (
          <div className="mt-4 border border-zinc-800 divide-y divide-zinc-800 max-h-64 overflow-y-auto">
            {backups.map((b) => (
              <div
                key={b.name}
                className="px-3 py-2 flex items-baseline justify-between gap-3"
              >
                <span className="text-xs text-zinc-200 font-mono truncate">
                  {b.name}
                </span>
                <div className="text-[10px] text-zinc-500 tabular-nums flex-shrink-0">
                  {b.created ? relTime(b.created) : "—"}
                  {b.size_bytes != null && (
                    <span className="ml-2">{fmtNumber(b.size_bytes)} B</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          !data?.error && (
            <div className="mt-4 text-[11px] text-zinc-500 border border-zinc-800 bg-zinc-800/20 px-3 py-2">
              {pitr
                ? "온디맨드 백업이 없습니다. PITR로 위 구간의 임의 시점 복원이 가능합니다."
                : "PITR가 비활성이고 온디맨드 백업이 없습니다. DynamoDB 백업은 AWS Console 또는 CDK에서 설정하세요 (현재 플랫폼은 읽기 전용)."}
            </div>
          )
        )}
      </div>
    );
  }

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
          {readOnly && (
            <span className="ml-2 px-1.5 py-0.5 bg-zinc-700/40 text-zinc-400 border border-zinc-700 text-[10px]">
              읽기 전용
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {admin && !readOnly && (
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
      {!readOnly && snapOpen && (
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

      {/* Inline restore form — admin-only, strongest gate (type-to-confirm).
          Opened from a snapshot row or the PITR section. */}
      {!readOnly && restoreMode && (
        <div className="mb-4 border border-rose-500/40 bg-rose-950/20 p-3">
          <div className="text-[11px] text-rose-200 mb-2">
            ⚠ 복원은 <strong>새 클러스터</strong>를 생성합니다 (과금 발생).
            소스 클러스터 <span className="font-mono">{clusterId}</span> 는
            변경되지 않습니다. 클러스터가 available 되면 writer 인스턴스가 자동
            생성되고 DBOps에 자동 등록됩니다 (수 분 소요).
          </div>
          <div className="text-[11px] text-zinc-400 mb-2">
            {restoreMode === "snapshot" ? (
              <>
                스냅샷{" "}
                <span className="font-mono text-zinc-200">{restoreSnapId}</span>{" "}
                에서 복원
              </>
            ) : (
              <>Point-in-Time 복원</>
            )}
          </div>

          {restoreMode === "pitr" && (
            <div className="mb-2 space-y-1.5">
              <label className="flex items-center gap-2 text-[11px] text-zinc-300">
                <input
                  type="checkbox"
                  checked={useLatest}
                  onChange={(e) => setUseLatest(e.target.checked)}
                />
                최신 복원 가능 시점으로 복원 (latest restorable time)
              </label>
              {!useLatest && (
                <input
                  type="datetime-local"
                  value={restoreToTime}
                  onChange={(e) => setRestoreToTime(e.target.value)}
                  className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 font-mono focus:outline-none focus:border-rose-500/60"
                />
              )}
            </div>
          )}

          <div className="space-y-2">
            <input
              type="text"
              value={newClusterId}
              onChange={(e) => setNewClusterId(e.target.value)}
              placeholder="새 클러스터 id"
              className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 font-mono focus:outline-none focus:border-rose-500/60"
            />
            <input
              type="text"
              value={confirmId}
              onChange={(e) => setConfirmId(e.target.value)}
              placeholder="확인을 위해 새 클러스터 id 를 다시 입력"
              className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 font-mono focus:outline-none focus:border-rose-500/60"
            />
          </div>

          <div className="flex items-center gap-2 mt-2">
            <button
              onClick={submitRestore}
              disabled={
                restoring ||
                !confirmMatches ||
                (restoreMode === "pitr" && !useLatest && !restoreToTime)
              }
              className="text-xs font-medium px-3 py-1.5 bg-rose-600 text-white hover:bg-rose-500 disabled:opacity-40 transition-colors"
            >
              {restoring ? "복원 시작 중…" : "복원 실행"}
            </button>
            <button
              onClick={() => {
                setRestoreMode(null);
                setRestoreError(null);
              }}
              className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
            >
              취소
            </button>
            {!confirmMatches && newClusterId.trim() && (
              <span className="text-[10px] text-zinc-500">
                확인 입력이 일치해야 실행됩니다
              </span>
            )}
          </div>
          {restoreError && (
            <div className="text-[11px] text-rose-300 mt-2">{restoreError}</div>
          )}
        </div>
      )}

      {data?.error && (
        <div
          className={`text-xs mb-3 px-3 py-2 border ${
            data.info
              ? "text-zinc-400 border-zinc-700 bg-zinc-800/30"
              : "text-rose-300 border-rose-500/40 bg-rose-500/10"
          }`}
        >
          {data.error}
        </div>
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
          {admin && !readOnly && (
            <button
              onClick={() => openRestore("pitr")}
              className="mt-2 text-[10px] px-2 py-1 border border-rose-500/40 text-rose-300 hover:bg-rose-500/10 transition-colors"
            >
              ↻ 시점으로 복원 (새 클러스터)
            </button>
          )}
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
                <SnapshotRow
                  key={s.id}
                  snapshot={s}
                  admin={admin && !readOnly}
                  onRestore={
                    s.status === "available"
                      ? () => openRestore("snapshot", s.id)
                      : undefined
                  }
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SnapshotRow({
  snapshot: s,
  admin,
  onRestore,
}: {
  snapshot: BackupSnapshot;
  admin?: boolean;
  onRestore?: () => void;
}) {
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
      <div className="flex items-center gap-2 flex-shrink-0">
        <div className="text-[10px] text-zinc-500 tabular-nums">
          {s.created ? relTime(s.created) : "—"}
          {s.allocated_storage_gb != null && (
            <span className="ml-2">{fmtNumber(s.allocated_storage_gb)} GB</span>
          )}
        </div>
        {admin && onRestore && (
          <button
            onClick={onRestore}
            title="이 스냅샷에서 새 클러스터로 복원"
            className="text-[10px] px-1.5 py-0.5 border border-rose-500/40 text-rose-300 hover:bg-rose-500/10 transition-colors"
          >
            복원
          </button>
        )}
      </div>
    </div>
  );
}
