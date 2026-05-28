"use client";

import { useCallback, useEffect, useState } from "react";
import {
  listMemoryRecords,
  deleteMemoryRecord,
  type MemoryKind,
  type MemoryRecord,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";

const KIND_OPTIONS: { value: MemoryKind; label: string; hint: string }[] = [
  {
    value: "preferences",
    label: "Preferences",
    hint: "Agent가 추론한 당신의 운영 스타일 — 선호하는 응답 어조, 분석 깊이, 자주 쓰는 명령 등",
  },
  {
    value: "facts",
    label: "Facts",
    hint: "Agent가 대화에서 추출한 사실 — 클러스터 환경, 도메인 지식, 반복되는 패턴",
  },
];

export default function PreferencesPage() {
  const [kind, setKind] = useState<MemoryKind>("preferences");
  const [records, setRecords] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    listMemoryRecords(kind)
      .then((d) => setRecords(d.records || []))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [kind]);

  useEffect(() => {
    load();
  }, [load]);

  const onDelete = async (rec: MemoryRecord) => {
    if (!confirm("이 기록을 삭제할까요? 이후 Agent는 이 정보를 잊습니다.")) {
      return;
    }
    setBusyId(rec.id);
    try {
      await deleteMemoryRecord(rec.id, kind);
      setRecords((prev) => prev.filter((r) => r.id !== rec.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  };

  const active = KIND_OPTIONS.find((o) => o.value === kind)!;

  return (
    <PageBody>
      <PageHeader
        eyebrow="설정"
        title="Agent가 기억하는 것"
        description="AgentCore Memory에 저장된 당신의 선호와 사실. 잘못된 정보가 박혀 있으면 여기서 삭제하세요 — 이후 대화부터 다시 학습됩니다."
        actions={
          <div className="flex border border-zinc-800">
            {KIND_OPTIONS.map((o) => {
              const a = kind === o.value;
              return (
                <button
                  key={o.value}
                  onClick={() => setKind(o.value)}
                  className={`text-xs px-4 py-2 transition-colors ${
                    a
                      ? "bg-zinc-100 text-zinc-950"
                      : "text-zinc-400 hover:text-zinc-200 bg-zinc-950"
                  }`}
                >
                  {o.label}
                </button>
              );
            })}
          </div>
        }
      />

      <Section
        eyebrow={active.label}
        title={`${records.length}개 기록`}
        description={active.hint}
      >
        {error && (
          <div className="mb-4 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
            {error}
          </div>
        )}
        {loading ? (
          <div className="text-sm text-zinc-500">불러오는 중…</div>
        ) : records.length === 0 ? (
          <EmptyState
            eyebrow={active.label}
            title="저장된 기록이 없습니다"
            description={
              kind === "preferences"
                ? "채팅을 진행하면 Agent가 당신의 선호 (응답 길이, 분석 스타일, 선호 명령)를 자동으로 추출해 여기에 누적합니다."
                : "Agent는 대화에서 사실을 추출해 여기에 누적합니다. 아직 학습이 충분치 않을 수 있어요."
            }
            secondary={{ href: "/chat", label: "Chat으로 이동" }}
          />
        ) : (
          <div className="border border-zinc-800 divide-y divide-zinc-800">
            {records.map((r) => (
              <div
                key={r.id}
                className="px-4 py-3 flex items-start justify-between gap-4 group"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-zinc-100 whitespace-pre-wrap break-words">
                    {r.content || "(empty)"}
                  </div>
                  <div className="flex items-center gap-3 mt-1 text-[10px] text-zinc-600 font-mono">
                    <span>{r.id.slice(0, 20)}…</span>
                    {r.updated_at && (
                      <span>· {new Date(r.updated_at).toLocaleString()}</span>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => onDelete(r)}
                  disabled={busyId === r.id}
                  className="opacity-0 group-hover:opacity-100 text-[11px] text-zinc-500 hover:text-rose-400 transition disabled:opacity-30"
                >
                  {busyId === r.id ? "삭제 중…" : "잊기"}
                </button>
              </div>
            ))}
          </div>
        )}
      </Section>
    </PageBody>
  );
}
