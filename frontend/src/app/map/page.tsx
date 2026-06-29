"use client";

// DB Map — service blueprint. Every registered DB grouped by the service/app it
// serves (service_tags), with auto-inferred facts + an admin-editable note.
// Clicking a card sets the GLOBAL selected cluster and opens its dashboard.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Boxes, Pencil, Check, X, Globe, Network } from "lucide-react";
import { fetchClusters, patchClusterMeta } from "@/lib/api-client";
import { setSelectedCluster } from "@/lib/selected-cluster";
import { isAdmin } from "@/lib/auth";
import { engineBadge } from "@/lib/engine";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";
import {
  groupByVpc,
  inferEnv,
  statusLevel,
  type MapCluster,
  type StatusLevel,
} from "@/lib/db-map";

const STATUS_DOT: Record<StatusLevel, string> = {
  ok: "bg-emerald-400",
  warning: "bg-amber-400",
  critical: "bg-rose-500",
};
const STATUS_TITLE: Record<StatusLevel, string> = {
  ok: "정상 (available)",
  warning: "주의 — 상태 전이 중",
  critical: "위험 — 중단/실패 상태",
};
const ENV_CHIP: Record<string, string> = {
  prod: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  staging: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  dev: "bg-slate-500/15 text-slate-300 border-slate-500/30",
};

function displayName(c: MapCluster): string {
  return (c as { resource_name?: string }).resource_name || c.cluster_id;
}

function DbCard({
  c,
  admin,
  onOpen,
  onSaved,
}: {
  c: MapCluster;
  admin: boolean;
  onOpen: (id: string) => void;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [purpose, setPurpose] = useState(c.purpose || "");
  const [tags, setTags] = useState((c.service_tags || []).join(", "));
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  const badge = engineBadge(c.engine);
  const env = inferEnv(displayName(c), c.service_tags);
  const level = statusLevel(c);

  const save = async () => {
    setSaving(true);
    setSaveErr(null);
    try {
      await patchClusterMeta(c.cluster_id, {
        purpose: purpose.trim(),
        service_tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      });
      setEditing(false);
      onSaved();
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return (
      <div className="rounded-xl border border-slate-700 bg-slate-900/60 p-4">
        <div className="mb-2 text-xs font-medium text-slate-400">
          {displayName(c)} — 노트 편집
        </div>
        <label className="mb-1 block text-[11px] text-slate-500">
          목적 (한 줄)
        </label>
        <input
          value={purpose}
          onChange={(e) => setPurpose(e.target.value)}
          maxLength={200}
          placeholder="예: 체크아웃 서비스 주 DB"
          className="mb-3 w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-200 focus:border-sky-500 focus:outline-none"
        />
        <label className="mb-1 block text-[11px] text-slate-500">
          연결 서비스 (쉼표로 구분)
        </label>
        <input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="checkout, order-worker"
          className="mb-3 w-full rounded-md border border-slate-700 bg-slate-950 px-2 py-1.5 text-sm text-slate-200 focus:border-sky-500 focus:outline-none"
        />
        {saveErr && <div className="mb-2 text-xs text-rose-400">{saveErr}</div>}
        <div className="flex gap-2">
          <button
            onClick={save}
            disabled={saving}
            className="inline-flex items-center gap-1 rounded-md bg-sky-600 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-sky-500 disabled:opacity-50"
          >
            <Check size={13} /> {saving ? "저장 중…" : "저장"}
          </button>
          <button
            onClick={() => {
              setEditing(false);
              setPurpose(c.purpose || "");
              setTags((c.service_tags || []).join(", "));
              setSaveErr(null);
            }}
            disabled={saving}
            className="inline-flex items-center gap-1 rounded-md border border-slate-700 px-2.5 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            <X size={13} /> 취소
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(c.cluster_id)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(c.cluster_id);
        }
      }}
      className="group relative cursor-pointer rounded-xl border border-slate-800 bg-slate-900/40 p-4 transition hover:border-sky-600/60 hover:bg-slate-900/80"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={`inline-block h-2 w-2 flex-shrink-0 rounded-full ${STATUS_DOT[level]}`}
            title={STATUS_TITLE[level]}
          />
          <span className="truncate font-medium text-slate-100">
            {displayName(c)}
          </span>
        </div>
        {admin && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setEditing(true);
            }}
            title="노트 편집"
            className="flex-shrink-0 rounded p-1 text-slate-500 opacity-0 transition hover:bg-slate-800 hover:text-slate-200 group-hover:opacity-100"
          >
            <Pencil size={13} />
          </button>
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <span
          className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${badge.classes}`}
        >
          {badge.short}
        </span>
        {env && (
          <span
            className={`rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase ${ENV_CHIP[env]}`}
          >
            {env}
          </span>
        )}
        {(c.service_tags || []).slice(0, 3).map((t) => (
          <span
            key={t}
            title="연결 서비스"
            className="rounded border border-violet-500/30 bg-violet-500/10 px-1.5 py-0.5 text-[10px] text-violet-300"
          >
            {t}
          </span>
        ))}
        {c.team_id && (
          <span className="text-[10px] text-slate-500">{c.team_id}</span>
        )}
      </div>

      {c.purpose ? (
        <p className="mt-2 line-clamp-2 text-xs text-slate-400">{c.purpose}</p>
      ) : (
        <p className="mt-2 text-xs italic text-slate-600">
          {admin ? "목적 미설정 — 편집으로 추가" : "목적 미설정"}
        </p>
      )}
    </div>
  );
}

export default function MapPage() {
  const router = useRouter();
  const [clusters, setClusters] = useState<MapCluster[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const admin = isAdmin();

  const load = useCallback(() => {
    fetchClusters()
      .then((r: unknown) => {
        const arr = Array.isArray(r)
          ? r
          : (r as { clusters?: MapCluster[] })?.clusters ?? [];
        setClusters(arr as MapCluster[]);
        setErr(null);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const groups = useMemo(() => groupByVpc(clusters), [clusters]);

  const open = useCallback(
    (id: string) => {
      setSelectedCluster(id);
      router.push(`/dashboard?cluster=${encodeURIComponent(id)}`);
    },
    [router],
  );

  return (
    <>
      <PageHeader
        eyebrow="Monitor"
        title="Map"
        description="계정의 DB를 Region → VPC로 묶어 본 아키텍처 청사진 — 노드를 클릭하면 해당 대시보드로 이동하고 전역 선택이 바뀝니다."
      />
      <PageBody>
        {loading ? (
          <div className="py-16 text-center text-sm text-slate-500">
            불러오는 중…
          </div>
        ) : err ? (
          <EmptyState
            title="클러스터를 불러오지 못했습니다"
            description={err}
          />
        ) : groups.length === 0 ? (
          <EmptyState
            title="등록된 DB가 없습니다"
            description="Clusters 페이지에서 클러스터를 먼저 등록하세요."
          />
        ) : (
          <div className="space-y-6">
            {groups.map((g, i) => {
              const prev = groups[i - 1];
              const newRegion = !prev || prev.region !== g.region;
              const serverless = g.vpcId === null;
              return (
                <div key={`${g.region}:${g.vpcId ?? "none"}`}>
                  {newRegion && (
                    <div className="mb-3 mt-1 flex items-center gap-2">
                      <Globe size={14} className="text-emerald-400" />
                      <h2 className="text-sm font-semibold tracking-tight text-emerald-300">
                        {g.region}
                      </h2>
                      <span className="text-[10px] uppercase tracking-wider text-slate-600">
                        region
                      </span>
                    </div>
                  )}
                  <section
                    className={`rounded-xl border p-4 ${
                      serverless
                        ? "border-dashed border-slate-800 bg-slate-900/20"
                        : "border-sky-900/50 bg-sky-950/10"
                    }`}
                  >
                    <div className="mb-3 flex flex-wrap items-center gap-2">
                      {serverless ? (
                        <Boxes size={14} className="text-slate-500" />
                      ) : (
                        <Network size={14} className="text-sky-400" />
                      )}
                      <h3
                        className={`font-mono text-xs font-medium ${
                          serverless ? "text-slate-500" : "text-sky-300"
                        }`}
                      >
                        {serverless ? "Serverless / VPC 외" : g.vpcId}
                      </h3>
                      {!serverless && g.azs.length > 0 && (
                        <span className="text-[10px] text-slate-500">
                          {g.azs.length} AZ · {g.azs.join(", ")}
                        </span>
                      )}
                      <span className="ml-auto rounded-full bg-slate-800 px-2 py-0.5 text-[10px] text-slate-400">
                        {g.clusters.length}
                      </span>
                    </div>
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                      {g.clusters.map((c) => (
                        <DbCard
                          key={`${g.vpcId ?? "none"}:${c.cluster_id}`}
                          c={c}
                          admin={admin}
                          onOpen={open}
                          onSaved={load}
                        />
                      ))}
                    </div>
                  </section>
                </div>
              );
            })}
          </div>
        )}
      </PageBody>
    </>
  );
}
