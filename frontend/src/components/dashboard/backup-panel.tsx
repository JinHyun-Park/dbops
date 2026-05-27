"use client";

interface ClusterMeta {
  backup_retention_days?: number | string | null;
  earliest_restorable_time?: string | null;
  latest_restorable_time?: string | null;
  preferred_backup_window?: string | null;
  preferred_maintenance_window?: string | null;
  multi_az?: boolean | null;
  deletion_protection?: boolean | null;
}

function relTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1) return "방금";
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  return `${Math.floor(h / 24)}일 전`;
}

function pitrWindow(earliest?: string | null, latest?: string | null): string {
  if (!earliest || !latest) return "-";
  const e = new Date(earliest).getTime();
  const l = new Date(latest).getTime();
  const hours = (l - e) / 3600000;
  if (hours < 24) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

export function BackupPanel({ cluster }: { cluster?: ClusterMeta }) {
  if (!cluster) return null;

  const retention = Number(cluster.backup_retention_days || 0);
  const retentionColor =
    retention < 1
      ? "text-rose-400"
      : retention < 7
        ? "text-amber-400"
        : "text-emerald-400";

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-4">
      <div className="text-xs text-zinc-400 uppercase tracking-wider mb-3">
        Backup & Maintenance
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <div>
          <div className="text-zinc-500 text-xs mb-1">Retention</div>
          <div className={`font-mono ${retentionColor}`}>{retention}d</div>
        </div>
        <div>
          <div className="text-zinc-500 text-xs mb-1">PITR Window</div>
          <div className="text-zinc-100 font-mono">
            {pitrWindow(
              cluster.earliest_restorable_time,
              cluster.latest_restorable_time,
            )}
          </div>
        </div>
        <div>
          <div className="text-zinc-500 text-xs mb-1">Latest Restore</div>
          <div className="text-zinc-300 text-xs">
            {relTime(cluster.latest_restorable_time)}
          </div>
        </div>
        <div>
          <div className="text-zinc-500 text-xs mb-1">Backup Window</div>
          <div className="text-zinc-300 font-mono text-xs">
            {cluster.preferred_backup_window || "-"}
          </div>
        </div>
        <div>
          <div className="text-zinc-500 text-xs mb-1">Maintenance Window</div>
          <div className="text-zinc-300 font-mono text-xs">
            {cluster.preferred_maintenance_window || "-"}
          </div>
        </div>
        <div>
          <div className="text-zinc-500 text-xs mb-1">Multi-AZ</div>
          <div
            className={cluster.multi_az ? "text-emerald-400" : "text-amber-400"}
          >
            {cluster.multi_az ? "enabled" : "disabled"}
          </div>
        </div>
        <div>
          <div className="text-zinc-500 text-xs mb-1">Deletion Protection</div>
          <div
            className={
              cluster.deletion_protection ? "text-emerald-400" : "text-rose-400"
            }
          >
            {cluster.deletion_protection ? "enabled" : "disabled"}
          </div>
        </div>
      </div>
    </div>
  );
}
