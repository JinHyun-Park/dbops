"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchEndpoints,
  type ClusterEndpoint,
  type EndpointsResponse,
} from "@/lib/api-client";

// Built-in writer/reader vs custom endpoints get distinct pill colors so the
// operator sees at a glance which are managed by AWS and which are theirs.
function typePill(type: string | null): { label: string; cls: string } {
  const t = (type || "").toUpperCase();
  if (t === "WRITER")
    return {
      label: "WRITER",
      cls: "bg-sky-500/15 text-sky-300 border-sky-500/40",
    };
  if (t === "READER")
    return {
      label: "READER",
      cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
    };
  if (t === "CUSTOM")
    return {
      label: "CUSTOM",
      cls: "bg-amber-500/15 text-amber-300 border-amber-500/40",
    };
  return {
    label: t || "—",
    cls: "bg-zinc-700/40 text-zinc-400 border-zinc-700",
  };
}

function statusColor(status: string | null): string {
  const s = (status || "").toLowerCase();
  if (s === "available") return "text-emerald-400";
  if (s === "creating" || s === "modifying") return "text-amber-400";
  if (s === "deleting") return "text-rose-400";
  return "text-zinc-400";
}

export function EndpointsPanel({ clusterId }: { clusterId: string }) {
  const [data, setData] = useState<EndpointsResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    fetchEndpoints(clusterId)
      .then(setData)
      .catch((e) =>
        setData({
          cluster_id: clusterId,
          endpoints: [],
          error: e instanceof Error ? e.message : String(e),
        }),
      )
      .finally(() => setLoading(false));
  }, [clusterId]);

  useEffect(() => {
    load();
  }, [load]);

  const endpoints = data?.endpoints ?? [];

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="text-sm text-zinc-200 font-medium">
          Cluster Endpoints
          {data && !data.error && data.custom_count != null && (
            <span className="ml-2 px-1.5 py-0.5 bg-amber-500/15 text-amber-300 border border-amber-500/30 text-[10px]">
              {data.custom_count} custom
            </span>
          )}
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="text-[10px] text-zinc-500 hover:text-zinc-300 disabled:opacity-50"
        >
          {loading ? "…" : "↻"}
        </button>
      </div>

      <div className="text-[11px] text-zinc-500 mb-3">
        커스텀 엔드포인트 생성·수정·삭제는 채팅으로 에이전트에게 요청하세요 (DBA
        승인 필요). 이 패널은 읽기 전용입니다.
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

      {endpoints.length > 0 ? (
        <div className="border border-zinc-800 divide-y divide-zinc-800">
          {endpoints.map((ep) => (
            <EndpointRow key={ep.identifier} ep={ep} />
          ))}
        </div>
      ) : (
        !data?.error && (
          <div className="text-[11px] text-zinc-500 border border-zinc-800 bg-zinc-800/20 px-3 py-2">
            엔드포인트가 없습니다.
          </div>
        )
      )}
    </div>
  );
}

function EndpointRow({ ep }: { ep: ClusterEndpoint }) {
  const pill = typePill(ep.type);
  const isCustom = (ep.type || "").toUpperCase() === "CUSTOM";
  const members = ep.static_members?.length
    ? { label: "포함", list: ep.static_members }
    : ep.excluded_members?.length
      ? { label: "제외", list: ep.excluded_members }
      : null;

  return (
    <div className="px-3 py-2.5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={`text-[10px] font-mono px-1 py-0.5 border ${pill.cls}`}
          >
            {pill.label}
            {isCustom && ep.custom_type ? ` · ${ep.custom_type}` : ""}
          </span>
          <span className="text-xs text-zinc-200 font-mono truncate">
            {ep.identifier}
          </span>
        </div>
        <span
          className={`text-[10px] font-mono flex-shrink-0 ${statusColor(
            ep.status,
          )}`}
        >
          {ep.status || "—"}
        </span>
      </div>
      {ep.endpoint && (
        <div className="text-[10px] text-zinc-500 font-mono truncate mt-1">
          {ep.endpoint}
        </div>
      )}
      {isCustom && members && (
        <div className="text-[10px] text-zinc-500 mt-1">
          <span className="text-zinc-600">{members.label}: </span>
          <span className="font-mono text-zinc-400">
            {members.list.join(", ")}
          </span>
        </div>
      )}
    </div>
  );
}
