"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchEngineConfig, type EngineConfigResponse } from "@/lib/api-client";
import { engineFamily } from "@/lib/engine";

// One config field rendered as a compact label + value cell. `tone` colors the
// value for boolean posture fields (on/off) so a DBA can scan protections at a
// glance without reading every word.
function ConfigCell({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: React.ReactNode;
  tone?: "neutral" | "good" | "warn" | "muted";
}) {
  const valueClass =
    tone === "good"
      ? "text-emerald-400"
      : tone === "warn"
        ? "text-amber-400"
        : tone === "muted"
          ? "text-zinc-500"
          : "text-zinc-100";
  return (
    <div>
      <div className="text-zinc-500 text-xs mb-1">{label}</div>
      <div className={`font-mono text-sm ${valueClass}`}>{value}</div>
    </div>
  );
}

function onOff(v: boolean | null | undefined): {
  text: string;
  tone: "good" | "muted";
} {
  return v ? { text: "활성", tone: "good" } : { text: "비활성", tone: "muted" };
}

export function EngineConfigPanel({
  clusterId,
  engine,
}: {
  clusterId: string;
  engine?: string;
}) {
  const [data, setData] = useState<EngineConfigResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    fetchEngineConfig(clusterId)
      .then(setData)
      .catch((e) =>
        setData({
          cluster_id: clusterId,
          error: e instanceof Error ? e.message : String(e),
        }),
      )
      .finally(() => setLoading(false));
  }, [clusterId]);

  useEffect(() => {
    load();
  }, [load]);

  const fam = engineFamily(engine);

  // Relational has the SettingsPanel — this panel is documentdb/dynamodb only.
  if (fam === "relational") return null;

  const header = (
    <div className="flex items-center justify-between mb-3">
      <div className="text-sm text-zinc-200 font-medium">
        Configuration
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
  );

  const errorBox = data?.error && (
    <div
      className={`text-xs mb-3 px-3 py-2 border ${
        data.info
          ? "text-zinc-400 border-zinc-700 bg-zinc-800/30"
          : "text-rose-300 border-rose-500/40 bg-rose-500/10"
      }`}
    >
      {data.error}
    </div>
  );

  // not_applicable (registry lookup failed, or relational fell through) →
  // neutral empty state.
  const notApplicable = data?.not_applicable && !data?.error;

  if (fam === "dynamodb") {
    const stream = onOff(data?.stream_enabled);
    const del = onOff(data?.deletion_protection_enabled);
    const ttlOn = data?.ttl_status === "ENABLED";
    return (
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        {header}
        {errorBox}
        {notApplicable ? (
          <div className="text-[11px] text-zinc-500 border border-zinc-800 bg-zinc-800/20 px-3 py-2">
            이 테이블의 구성 정보를 표시할 수 없습니다.
          </div>
        ) : (
          !data?.error && (
            <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
              <ConfigCell
                label="Table Class"
                value={data?.table_class || "STANDARD"}
              />
              <ConfigCell
                label="삭제 방지 (Deletion Protection)"
                value={del.text}
                tone={del.tone}
              />
              <ConfigCell
                label="암호화 (SSE)"
                value={
                  data?.sse_status
                    ? `${data.sse_type || "AWS owned"} · ${data.sse_status}`
                    : "AWS 소유 키 (기본)"
                }
                tone={data?.sse_status ? "good" : "neutral"}
              />
              <ConfigCell
                label="DynamoDB Streams"
                value={
                  stream.text === "활성" && data?.stream_view_type
                    ? `활성 · ${data.stream_view_type}`
                    : stream.text
                }
                tone={stream.tone}
              />
              <ConfigCell
                label="TTL"
                value={
                  ttlOn && data?.ttl_attribute_name
                    ? `활성 · ${data.ttl_attribute_name}`
                    : data?.ttl_status === "ENABLED"
                      ? "활성"
                      : "비활성"
                }
                tone={ttlOn ? "good" : "muted"}
              />
            </div>
          )
        )}
      </div>
    );
  }

  if (fam === "elasticache") {
    const inTransit = onOff(data?.transit_encryption_enabled);
    // At-rest: prefer the encryption TYPE (authoritative — a node can be
    // encrypted even when the legacy boolean reads false) and surface it.
    const atRestOn = !!data?.at_rest_encryption_enabled;
    const atRestText = atRestOn
      ? data?.storage_encryption_type
        ? `활성 · ${data.storage_encryption_type}`
        : "활성"
      : "비활성";
    // AUTH: a legacy auth token OR RBAC user groups both mean "authenticated".
    const authToken = !!data?.auth_enabled;
    const rbac = !!data?.rbac_enabled;
    const authText = authToken
      ? "활성 (토큰)"
      : rbac
        ? "활성 (RBAC)"
        : "비활성";
    const params = data?.parameters || {};
    const PARAM_LABELS: Record<string, string> = {
      "maxmemory-policy": "Eviction Policy (maxmemory-policy)",
      "reserved-memory-percent": "Reserved Memory %",
      "maxmemory-samples": "maxmemory-samples",
      timeout: "Idle Timeout (timeout)",
      "tcp-keepalive": "TCP Keepalive",
      databases: "Databases",
      "cluster-enabled": "Cluster Mode (cluster-enabled)",
      "slowlog-log-slower-than": "Slowlog Threshold (µs)",
      max_item_size: "Max Item Size",
    };
    const paramKeys = Object.keys(PARAM_LABELS).filter(
      (k) => params[k] != null,
    );
    const retention = data?.snapshot_retention_limit;
    return (
      <div className="bg-zinc-900/50 border border-zinc-800 p-5">
        {header}
        {errorBox}
        {notApplicable ? (
          <div className="text-[11px] text-zinc-500 border border-zinc-800 bg-zinc-800/20 px-3 py-2">
            이 클러스터의 구성 정보를 표시할 수 없습니다.
          </div>
        ) : (
          !data?.error && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                <ConfigCell
                  label="파라미터 그룹"
                  value={data?.parameter_group || "—"}
                />
                <ConfigCell
                  label="유지보수 윈도우 (Maintenance Window)"
                  value={data?.preferred_maintenance_window || "—"}
                />
                <ConfigCell
                  label="스냅샷 보관 (Retention)"
                  value={
                    retention != null
                      ? retention > 0
                        ? `${retention}d`
                        : "비활성"
                      : "—"
                  }
                  tone={retention ? "neutral" : "muted"}
                />
                <ConfigCell
                  label="스냅샷 윈도우 (Snapshot Window)"
                  value={data?.snapshot_window || "—"}
                />
                <ConfigCell
                  label="저장 시 암호화 (At-Rest)"
                  value={atRestText}
                  tone={atRestOn ? "good" : "muted"}
                />
                <ConfigCell
                  label="전송 중 암호화 (In-Transit / TLS)"
                  value={inTransit.text}
                  tone={inTransit.tone}
                />
                <ConfigCell
                  label="AUTH"
                  value={authText}
                  tone={authToken || rbac ? "good" : "muted"}
                />
                <ConfigCell
                  label="자동 Failover"
                  value={data?.automatic_failover || "—"}
                  tone={
                    data?.automatic_failover === "enabled" ? "good" : "muted"
                  }
                />
                <ConfigCell
                  label="Multi-AZ"
                  value={data?.multi_az || "—"}
                  tone={data?.multi_az === "enabled" ? "good" : "muted"}
                />
              </div>
              {paramKeys.length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-medium mb-2 pt-2 border-t border-zinc-800/80">
                    파라미터 (parameter group)
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                    {paramKeys.map((k) => {
                      const v = params[k];
                      // noeviction = writes fail once memory is full — flag it.
                      const risky =
                        k === "maxmemory-policy" && v === "noeviction";
                      return (
                        <ConfigCell
                          key={k}
                          label={PARAM_LABELS[k]}
                          value={v ?? "—"}
                          tone={risky ? "warn" : "neutral"}
                        />
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          )
        )}
      </div>
    );
  }

  // ── DocumentDB ──
  const del = onOff(data?.deletion_protection);
  const encrypted = onOff(data?.storage_encrypted);
  const retention = data?.backup_retention_period;
  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-5">
      {header}
      {errorBox}
      {notApplicable ? (
        <div className="text-[11px] text-zinc-500 border border-zinc-800 bg-zinc-800/20 px-3 py-2">
          이 클러스터의 구성 정보를 표시할 수 없습니다.
        </div>
      ) : (
        !data?.error && (
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
            <ConfigCell
              label="유지보수 윈도우 (Maintenance Window)"
              value={data?.preferred_maintenance_window || "—"}
            />
            <ConfigCell
              label="삭제 방지 (Deletion Protection)"
              value={del.text}
              tone={del.tone}
            />
            <ConfigCell
              label="스토리지 암호화 (Encrypted)"
              value={encrypted.text}
              tone={encrypted.tone}
            />
            <ConfigCell
              label="클러스터 파라미터 그룹"
              value={data?.db_cluster_parameter_group || "—"}
            />
            <ConfigCell
              label="백업 보관 기간 (Retention)"
              value={retention != null ? `${retention}d` : "—"}
            />
          </div>
        )
      )}
    </div>
  );
}
