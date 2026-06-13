"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import {
  fetchClusters,
  fetchRunbook,
  fetchRunbooks,
  createRunbook,
  deleteRunbook,
  type RunbookDetail,
  type RunbookListItem,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  EmptyState,
  Section,
} from "@/components/design-system/page-shell";
import { getSelectedCluster } from "@/lib/selected-cluster";
import { SearchableClusterSelect } from "@/components/design-system/searchable-cluster-select";

interface ClusterLite {
  cluster_id: string;
}

// Storage key for the form draft so a half-typed runbook survives an
// accidental navigation. Cleared on successful POST.
const DRAFT_KEY = "dbops_runbook_draft";

interface RunbookDraft {
  title: string;
  cluster_id: string;
  summary_md: string;
  body_md: string;
  tags_csv: string;
}

const DEFAULT_DRAFT: RunbookDraft = {
  title: "",
  cluster_id: "",
  summary_md: "",
  body_md: "",
  tags_csv: "",
};

export default function RunbooksPage() {
  const router = useRouter();
  const [clusters, setClusters] = useState<ClusterLite[]>([]);
  // Seed from the global cluster selection so runbooks open scoped to the
  // cluster the DBA is focused on; "" = all clusters when none is selected.
  const [filterCluster, setFilterCluster] = useState<string>(
    () => getSelectedCluster() ?? "",
  );
  const [filterTag, setFilterTag] = useState<string>("");
  const [items, setItems] = useState<RunbookListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<RunbookDetail | null>(null);
  const [showForm, setShowForm] = useState(false);

  useEffect(() => {
    fetchClusters()
      .then((r: unknown) => {
        const list: ClusterLite[] = Array.isArray(r)
          ? (r as ClusterLite[])
          : (r as { clusters?: ClusterLite[] })?.clusters ?? [];
        setClusters(list);
      })
      .catch(() => {});
  }, []);

  const load = useCallback(() => {
    setLoading(true);
    setErr(null);
    fetchRunbooks({
      clusterId: filterCluster || undefined,
      tag: filterTag || undefined,
    })
      .then((r) => setItems(r.runbooks))
      .catch((e) => setErr(e instanceof Error ? e.message : "fetch failed"))
      .finally(() => setLoading(false));
  }, [filterCluster, filterTag]);

  useEffect(() => {
    load();
  }, [load]);

  // Hand a runbook off to the agent: navigate to /chat with a pre-filled
  // prompt. The chat page reads the `prompt` searchParam on mount and seeds
  // the input. The agent then uses the get_runbook tool to pull the steps
  // and runs each SQL step through the approval-gated execute_sql flow.
  const runWithAgent = useCallback(
    (rb: RunbookListItem) => {
      const prompt = `다음 런북을 단계별로 검토하고 실행해줘: ${rb.title} (id=${rb.id})`;
      router.push(`/chat?prompt=${encodeURIComponent(prompt)}`);
    },
    [router],
  );

  return (
    <PageBody>
      <PageHeader
        eyebrow="자동화"
        title="Runbooks"
        description="AI 진단 + 권장 조치를 재사용 가능한 playbook으로 저장. 동일 패턴 재발 시 같은 처방을 곧바로 참조합니다."
        actions={
          <button
            type="button"
            onClick={() => setShowForm((v) => !v)}
            className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
          >
            {showForm ? "× 작성 닫기" : "+ 새 Runbook"}
          </button>
        }
      />

      {showForm && (
        <Section eyebrow="새 Runbook" title="수동 작성">
          <ManualForm
            clusters={clusters}
            onCreated={() => {
              setShowForm(false);
              load();
            }}
          />
        </Section>
      )}

      <Section
        eyebrow="필터"
        title="등록된 Runbook"
        description={`총 ${items.length}개`}
        actions={
          <div className="flex items-center gap-2">
            <SearchableClusterSelect
              value={filterCluster}
              onChange={setFilterCluster}
              clusters={clusters}
              allowAll
              allLabel="모든 클러스터"
              className="w-48"
            />
            <input
              type="text"
              value={filterTag}
              onChange={(e) => setFilterTag(e.target.value)}
              placeholder="태그"
              className="bg-zinc-900 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-28"
            />
          </div>
        }
      >
        {err && (
          <div className="text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2 mb-3">
            {err}
          </div>
        )}
        {loading ? (
          <div className="text-zinc-500 text-sm">불러오는 중…</div>
        ) : items.length === 0 ? (
          <EmptyState
            title="저장된 Runbook 없음"
            description={
              filterCluster || filterTag
                ? "필터를 비우거나 다른 클러스터를 선택해보세요."
                : "Chat에서 AI 진단을 받은 뒤 '✓ Runbook 저장' 버튼으로 저장하거나, 위의 '+ 새 Runbook'으로 수동 작성하세요."
            }
          />
        ) : (
          <ul className="divide-y divide-zinc-800 border border-zinc-800 bg-zinc-900/40">
            {items.map((rb) => (
              <li
                key={rb.id}
                className="flex items-stretch hover:bg-zinc-800/30 transition-colors"
              >
                <button
                  type="button"
                  onClick={async () => {
                    try {
                      const d = await fetchRunbook(rb.id);
                      setSelected(d);
                    } catch (e) {
                      setErr(e instanceof Error ? e.message : "fetch failed");
                    }
                  }}
                  className="flex-1 min-w-0 text-left px-4 py-3"
                >
                  <div className="flex items-baseline justify-between gap-3 flex-wrap">
                    <span className="text-sm text-zinc-200 font-medium">
                      {rb.title}
                    </span>
                    <span className="text-[10px] text-zinc-500 font-mono">
                      {new Date(rb.created_at).toLocaleString()} ·{" "}
                      {rb.created_by ?? "anonymous"}
                    </span>
                  </div>
                  {rb.summary_md && (
                    <div className="text-xs text-zinc-400 mt-1 line-clamp-2">
                      {rb.summary_md}
                    </div>
                  )}
                  <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    {rb.cluster_id && (
                      <span className="text-[10px] font-mono text-zinc-500">
                        {rb.cluster_id}
                      </span>
                    )}
                    {rb.source && (
                      <span className="text-[10px] px-1.5 py-0.5 border border-zinc-700 text-zinc-400 font-mono uppercase tracking-wider">
                        {rb.source}
                      </span>
                    )}
                    {rb.tags.map((t) => (
                      <span
                        key={t}
                        className="text-[10px] px-1.5 py-0.5 bg-amber-500/10 text-amber-300 border border-amber-500/40 font-mono"
                      >
                        #{t}
                      </span>
                    ))}
                  </div>
                </button>
                <div className="flex items-center pr-3 pl-1 shrink-0">
                  <button
                    type="button"
                    onClick={() => runWithAgent(rb)}
                    title="이 Runbook을 채팅으로 가져가 에이전트가 단계별로 검토·실행 (쓰기는 승인 필요)"
                    className="text-[11px] px-2.5 py-1.5 border border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 font-mono whitespace-nowrap transition-colors"
                  >
                    ▶ 에이전트로 실행
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {selected && (
        <RunbookModal
          runbook={selected}
          onClose={() => setSelected(null)}
          onDeleted={() => {
            setSelected(null);
            load();
          }}
        />
      )}
    </PageBody>
  );
}

// ---------------------------------------------------------------------------
// Create form
// ---------------------------------------------------------------------------

function ManualForm({
  clusters,
  onCreated,
}: {
  clusters: ClusterLite[];
  onCreated: () => void;
}) {
  // Hydrate from localStorage draft so a half-typed form survives nav.
  const [draft, setDraft] = useState<RunbookDraft>(() => {
    if (typeof window === "undefined") return DEFAULT_DRAFT;
    try {
      const raw = window.localStorage.getItem(DRAFT_KEY);
      return raw
        ? { ...DEFAULT_DRAFT, ...(JSON.parse(raw) as Partial<RunbookDraft>) }
        : DEFAULT_DRAFT;
    } catch {
      return DEFAULT_DRAFT;
    }
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    try {
      window.localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    } catch {
      /* ignore */
    }
  }, [draft]);

  const update = (patch: Partial<typeof DEFAULT_DRAFT>) =>
    setDraft((d) => ({ ...d, ...patch }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (!draft.title.trim() || !draft.body_md.trim()) {
      setErr("제목과 본문은 필수입니다");
      return;
    }
    setBusy(true);
    try {
      const tags = draft.tags_csv
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);
      await createRunbook({
        cluster_id: draft.cluster_id || undefined,
        title: draft.title.trim(),
        summary_md: draft.summary_md.trim() || undefined,
        body_md: draft.body_md,
        tags,
        source: "manual",
      });
      try {
        window.localStorage.removeItem(DRAFT_KEY);
      } catch {
        /* ignore */
      }
      setDraft(DEFAULT_DRAFT);
      onCreated();
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "create failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      onSubmit={submit}
      className="border border-zinc-800 bg-zinc-900/40 p-5 space-y-3"
    >
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <label className="block">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
            제목
          </div>
          <input
            value={draft.title}
            onChange={(e) => update({ title: e.target.value })}
            placeholder="예: idle-in-tx 누적 시 자동 cleanup"
            className="w-full bg-zinc-950 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5 font-mono"
          />
        </label>
        <label className="block">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
            Cluster (선택)
          </div>
          <select
            value={draft.cluster_id}
            onChange={(e) => update({ cluster_id: e.target.value })}
            className="w-full bg-zinc-950 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5 font-mono"
          >
            <option value="">(클러스터 무관)</option>
            {clusters.map((c) => (
              <option key={c.cluster_id} value={c.cluster_id}>
                {c.cluster_id}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="block">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
          요약 (선택, 한 줄)
        </div>
        <input
          value={draft.summary_md}
          onChange={(e) => update({ summary_md: e.target.value })}
          placeholder="목록에 표시되는 한 줄 요약"
          className="w-full bg-zinc-950 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5"
        />
      </label>
      <label className="block">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
          본문 (Markdown)
        </div>
        <textarea
          value={draft.body_md}
          onChange={(e) => update({ body_md: e.target.value })}
          rows={10}
          placeholder={"## 증상\n...\n\n## 진단\n...\n\n## 조치\n```sql\n..."}
          className="w-full bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-3 py-2 font-mono resize-y"
        />
      </label>
      <label className="block">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
          태그 (콤마 구분, 최대 16개)
        </div>
        <input
          value={draft.tags_csv}
          onChange={(e) => update({ tags_csv: e.target.value })}
          placeholder="high-cpu, autovacuum, idle-in-tx"
          className="w-full bg-zinc-950 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5 font-mono"
        />
      </label>
      {err && (
        <div className="text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
          {err}
        </div>
      )}
      <div className="flex items-center justify-end gap-2">
        <span className="text-[10px] text-zinc-600">
          작성 중 내용은 자동으로 브라우저에 저장됩니다.
        </span>
        <button
          type="submit"
          disabled={busy}
          className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors"
        >
          {busy ? "저장 중…" : "Runbook 저장"}
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Detail modal
// ---------------------------------------------------------------------------

// Export a runbook as a portable Markdown file (YAML front-matter + body) so it
// can be moved into git / a wiki / an incident ticket. Browser Blob download —
// no new dependency. For PDF, the browser's own "Print → Save as PDF" on the
// rendered modal is the path; we don't ship a fragile print-CSS hack.
function exportRunbookMarkdown(rb: RunbookDetail) {
  const fm = [
    "---",
    `title: ${JSON.stringify(rb.title)}`,
    rb.cluster_id ? `cluster: ${rb.cluster_id}` : null,
    `created: ${rb.created_at}`,
    rb.created_by ? `author: ${rb.created_by}` : null,
    rb.tags.length ? `tags: [${rb.tags.join(", ")}]` : null,
    rb.source ? `source: ${rb.source}` : null,
    "---",
    "",
  ]
    .filter(Boolean)
    .join("\n");
  const blob = new Blob([fm + (rb.body_md || "")], {
    type: "text/markdown;charset=utf-8",
  });
  const slug =
    (rb.title || "runbook")
      .toLowerCase()
      .replace(/[^a-z0-9가-힣]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "runbook";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${slug}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function RunbookModal({
  runbook,
  onClose,
  onDeleted,
}: {
  runbook: RunbookDetail;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Esc closes the modal — standard expectation
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const remove = async () => {
    setBusy(true);
    setErr(null);
    try {
      await deleteRunbook(runbook.id);
      onDeleted();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "delete failed");
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-6"
      onClick={onClose}
    >
      <div
        className="bg-zinc-950 border border-zinc-800 max-w-3xl w-full max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-4 border-b border-zinc-800 flex items-baseline justify-between gap-3 flex-wrap">
          <div>
            <div className="text-base text-zinc-100 font-semibold">
              {runbook.title}
            </div>
            <div className="text-[11px] text-zinc-500 mt-0.5 font-mono">
              {runbook.cluster_id ? `${runbook.cluster_id} · ` : ""}
              {new Date(runbook.created_at).toLocaleString()} ·{" "}
              {runbook.created_by ?? "anonymous"}
              {runbook.source ? ` · ${runbook.source}` : ""}
            </div>
            {runbook.tags.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                {runbook.tags.map((t) => (
                  <span
                    key={t}
                    className="text-[10px] px-1.5 py-0.5 bg-amber-500/10 text-amber-300 border border-amber-500/40 font-mono"
                  >
                    #{t}
                  </span>
                ))}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-200 text-xs"
          >
            ✕ 닫기
          </button>
        </div>
        <div className="px-5 py-4 overflow-y-auto flex-1">
          <article className="prose prose-invert prose-sm max-w-none">
            <ReactMarkdown>{runbook.body_md}</ReactMarkdown>
          </article>
        </div>
        <div className="px-5 py-3 border-t border-zinc-800 flex items-center justify-between gap-3">
          {err && <div className="text-xs text-rose-300">{err}</div>}
          <div className="ml-auto flex items-center gap-3">
            <button
              type="button"
              onClick={() => exportRunbookMarkdown(runbook)}
              className="text-xs text-zinc-300 hover:text-zinc-100 transition-colors"
              title="YAML front-matter + 본문을 .md 파일로 내보냅니다"
            >
              ⬇ Markdown 내보내기
            </button>
            <span className="text-zinc-700">·</span>
            {confirmDelete ? (
              <>
                <button
                  type="button"
                  onClick={() => setConfirmDelete(false)}
                  disabled={busy}
                  className="text-xs text-zinc-400 px-2 py-1"
                >
                  취소
                </button>
                <button
                  type="button"
                  onClick={remove}
                  disabled={busy}
                  className="text-xs px-3 py-1 bg-rose-500 text-zinc-950 hover:bg-rose-400 disabled:opacity-50 transition-colors"
                >
                  {busy ? "삭제 중…" : "정말 삭제"}
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmDelete(true)}
                className="text-xs text-rose-400 hover:text-rose-300"
              >
                삭제
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
