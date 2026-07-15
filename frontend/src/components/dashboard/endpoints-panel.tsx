"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchEndpoints,
  createEndpointRequest,
  type ClusterEndpoint,
  type EndpointsResponse,
  type EndpointAction,
} from "@/lib/api-client";
import { isAdmin } from "@/lib/auth";

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

// Members are typed as instance ids separated by commas or whitespace. The
// operations create tool validates them against the real cluster members and
// returns the valid set on mismatch, so we don't need the instance list here.
function parseMembers(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function EndpointsPanel({ clusterId }: { clusterId: string }) {
  const [data, setData] = useState<EndpointsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [admin, setAdmin] = useState(false);

  // Create-form state (admin-only). memberMode picks which mutually-exclusive
  // list the typed ids populate; "none" = all readers (no member restriction).
  const [createOpen, setCreateOpen] = useState(false);
  const [newId, setNewId] = useState("");
  const [newType, setNewType] = useState<"READER" | "ANY">("READER");
  const [newMemberMode, setNewMemberMode] = useState<
    "none" | "static" | "excluded"
  >("none");
  const [newMembers, setNewMembers] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    setAdmin(isAdmin());
  }, []);

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

  // Shared submit for all three actions. On success we DON'T mutate the list
  // optimistically — the change only lands after the DBA approves + it executes.
  const submit = useCallback(
    async (opts: {
      action: EndpointAction;
      endpointIdentifier: string;
      endpointType?: "READER" | "ANY";
      staticMembers?: string[];
      excludedMembers?: string[];
    }) => {
      setSubmitting(true);
      setError(null);
      try {
        const r = await createEndpointRequest({ clusterId, ...opts });
        setToast(r.message);
        setCreateOpen(false);
        setNewId("");
        setNewMembers("");
        setNewMemberMode("none");
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return false;
      } finally {
        setSubmitting(false);
      }
    },
    [clusterId],
  );

  const submitCreate = useCallback(() => {
    const members = parseMembers(newMembers);
    return submit({
      action: "create_custom_endpoint",
      endpointIdentifier: newId.trim(),
      endpointType: newType,
      staticMembers: newMemberMode === "static" ? members : undefined,
      excludedMembers: newMemberMode === "excluded" ? members : undefined,
    });
  }, [submit, newMembers, newId, newType, newMemberMode]);

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
        <div className="flex items-center gap-3">
          {admin && (
            <button
              onClick={() => {
                setError(null);
                setCreateOpen((v) => !v);
              }}
              className="text-[10px] px-2 py-1 border border-zinc-700 text-zinc-300 hover:border-amber-500/60 hover:text-amber-200 transition-colors"
            >
              + 커스텀 엔드포인트 추가
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

      {admin ? (
        <div className="text-[11px] text-zinc-500 mb-3">
          커스텀 엔드포인트 생성·수정·삭제는 DBA 승인이 필요합니다. 요청하면
          승인 센터에 등록되고, 승인 즉시 실행됩니다.
        </div>
      ) : (
        <div className="text-[11px] text-zinc-500 mb-3">
          커스텀 엔드포인트 변경은 관리자(DBA)만 요청할 수 있습니다. 이 패널은
          읽기 전용입니다.
        </div>
      )}

      {toast && (
        <div className="mb-3 px-3 py-2 border border-emerald-500/40 bg-emerald-500/10 text-emerald-200 text-xs flex items-start justify-between gap-3">
          <span>
            {toast}{" "}
            <a
              href="/approvals"
              className="underline hover:text-emerald-100 font-medium"
            >
              승인 센터로 이동
            </a>
          </span>
          <button
            onClick={() => setToast(null)}
            className="text-emerald-300/70 hover:text-emerald-200 flex-shrink-0"
          >
            ✕
          </button>
        </div>
      )}

      {/* Inline create form — admin only */}
      {admin && createOpen && (
        <div className="mb-4 border border-zinc-800 bg-zinc-950 p-3 space-y-2">
          <div className="text-[11px] text-zinc-400">
            READER 는 읽기 전용 리더만, ANY 는 writer·reader 모두 라우팅
            대상입니다. 멤버를 지정하지 않으면 모든 리더가 포함됩니다.
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={newId}
              onChange={(e) => setNewId(e.target.value)}
              placeholder="endpoint identifier"
              className="flex-1 min-w-[160px] bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 font-mono focus:outline-none focus:border-amber-500/60"
            />
            <select
              value={newType}
              onChange={(e) => setNewType(e.target.value as "READER" | "ANY")}
              className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 focus:outline-none focus:border-amber-500/60"
            >
              <option value="READER">READER</option>
              <option value="ANY">ANY</option>
            </select>
          </div>
          <MemberInput
            mode={newMemberMode}
            onMode={setNewMemberMode}
            value={newMembers}
            onValue={setNewMembers}
          />
          <div className="flex items-center gap-2">
            <button
              onClick={submitCreate}
              disabled={submitting || !newId.trim()}
              className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors"
            >
              {submitting ? "요청 중…" : "승인 요청"}
            </button>
            <button
              onClick={() => {
                setCreateOpen(false);
                setError(null);
              }}
              className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
            >
              취소
            </button>
          </div>
          {error && <div className="text-[11px] text-rose-300">{error}</div>}
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

      {endpoints.length > 0 ? (
        <div className="border border-zinc-800 divide-y divide-zinc-800">
          {endpoints.map((ep) => (
            <EndpointRow
              key={ep.identifier}
              ep={ep}
              admin={admin}
              submitting={submitting}
              error={error}
              onSubmit={submit}
              clearError={() => setError(null)}
            />
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

// Reusable member-mode selector + id input, shared by create + edit forms.
function MemberInput({
  mode,
  onMode,
  value,
  onValue,
}: {
  mode: "none" | "static" | "excluded";
  onMode: (m: "none" | "static" | "excluded") => void;
  value: string;
  onValue: (v: string) => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-3 text-[11px] text-zinc-300">
        {(
          [
            ["none", "전체 리더"],
            ["static", "포함(static)"],
            ["excluded", "제외(excluded)"],
          ] as const
        ).map(([m, label]) => (
          <label key={m} className="flex items-center gap-1 cursor-pointer">
            <input
              type="radio"
              checked={mode === m}
              onChange={() => onMode(m)}
            />
            {label}
          </label>
        ))}
      </div>
      {mode !== "none" && (
        <input
          type="text"
          value={value}
          onChange={(e) => onValue(e.target.value)}
          placeholder="인스턴스 id (쉼표/공백 구분)"
          className="w-full bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs px-2 py-1.5 font-mono focus:outline-none focus:border-amber-500/60"
        />
      )}
    </div>
  );
}

function EndpointRow({
  ep,
  admin,
  submitting,
  error,
  onSubmit,
  clearError,
}: {
  ep: ClusterEndpoint;
  admin: boolean;
  submitting: boolean;
  error: string | null;
  onSubmit: (opts: {
    action: EndpointAction;
    endpointIdentifier: string;
    staticMembers?: string[];
    excludedMembers?: string[];
  }) => Promise<boolean>;
  clearError: () => void;
}) {
  const pill = typePill(ep.type);
  const isCustom = (ep.type || "").toUpperCase() === "CUSTOM";
  const members = ep.static_members?.length
    ? { label: "포함", list: ep.static_members }
    : ep.excluded_members?.length
      ? { label: "제외", list: ep.excluded_members }
      : null;

  // Per-row editors. Only one of edit/deleteConfirm is open at a time.
  const [editOpen, setEditOpen] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [editMode, setEditMode] = useState<"static" | "excluded">(
    ep.excluded_members?.length ? "excluded" : "static",
  );
  const [editMembers, setEditMembers] = useState(
    (ep.static_members?.length
      ? ep.static_members
      : ep.excluded_members ?? []
    ).join(", "),
  );

  const id = ep.identifier || "";

  const submitEdit = async () => {
    const list = parseMembers(editMembers);
    const ok = await onSubmit({
      action: "modify_custom_endpoint",
      endpointIdentifier: id,
      staticMembers: editMode === "static" ? list : undefined,
      excludedMembers: editMode === "excluded" ? list : undefined,
    });
    if (ok) setEditOpen(false);
  };

  const submitDelete = async () => {
    const ok = await onSubmit({
      action: "delete_custom_endpoint",
      endpointIdentifier: id,
    });
    if (ok) setDeleteConfirm(false);
  };

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
        <div className="flex items-center gap-2 flex-shrink-0">
          {isCustom && admin && (
            <>
              <button
                onClick={() => {
                  clearError();
                  setDeleteConfirm(false);
                  setEditOpen((v) => !v);
                }}
                className="text-[10px] px-1.5 py-0.5 border border-zinc-700 text-zinc-300 hover:border-amber-500/60 hover:text-amber-200 transition-colors"
              >
                멤버 편집
              </button>
              <button
                onClick={() => {
                  clearError();
                  setEditOpen(false);
                  setDeleteConfirm((v) => !v);
                }}
                className="text-[10px] px-1.5 py-0.5 border border-rose-500/40 text-rose-300 hover:bg-rose-500/10 transition-colors"
              >
                삭제
              </button>
            </>
          )}
          <span className={`text-[10px] font-mono ${statusColor(ep.status)}`}>
            {ep.status || "—"}
          </span>
        </div>
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

      {/* Inline edit form */}
      {editOpen && (
        <div className="mt-2 border border-zinc-800 bg-zinc-950 p-2.5 space-y-2">
          <MemberInput
            mode={editMode}
            onMode={(m) =>
              setEditMode(m === "excluded" ? "excluded" : "static")
            }
            value={editMembers}
            onValue={setEditMembers}
          />
          <div className="flex items-center gap-2">
            <button
              onClick={submitEdit}
              disabled={submitting}
              className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors"
            >
              {submitting ? "요청 중…" : "승인 요청"}
            </button>
            <button
              onClick={() => setEditOpen(false)}
              className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
            >
              취소
            </button>
          </div>
          {error && <div className="text-[11px] text-rose-300">{error}</div>}
        </div>
      )}

      {/* Inline delete confirm — no browser confirm() */}
      {deleteConfirm && (
        <div className="mt-2 border border-rose-500/40 bg-rose-950/20 p-2.5 space-y-2">
          <div className="text-[11px] text-rose-200">
            커스텀 엔드포인트 <span className="font-mono">{ep.identifier}</span>{" "}
            삭제 승인을 요청합니다. writer/reader 내장 엔드포인트는 영향받지
            않습니다.
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={submitDelete}
              disabled={submitting}
              className="text-xs font-medium px-3 py-1.5 bg-rose-600 text-white hover:bg-rose-500 disabled:opacity-50 transition-colors"
            >
              {submitting ? "요청 중…" : "삭제 승인 요청"}
            </button>
            <button
              onClick={() => setDeleteConfirm(false)}
              className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
            >
              취소
            </button>
          </div>
          {error && <div className="text-[11px] text-rose-300">{error}</div>}
        </div>
      )}
    </div>
  );
}
