"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchContextFiles,
  uploadContextFile,
  deleteContextFile,
  type ContextFile,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";
import { fmtBytes } from "@/lib/format";

// ── Helpers ──────────────────────────────────────────────────────────────────

const ALLOWED_EXTENSIONS = new Set(["md", "txt", "csv"]);
const MAX_BYTES = 32_768; // per-file cap
const PER_FILE_MAX_BYTES = MAX_BYTES;
const TOTAL_BUDGET_BYTES = 65_536; // total budget
const RESERVED_MARKER = "OPERATOR_CONTEXT";

function extOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function contentTypeOf(ext: string): string {
  if (ext === "md") return "text/markdown";
  if (ext === "csv") return "text/csv";
  return "text/plain";
}

function fmtTs(ts?: string): string | null {
  if (!ts) return null;
  try {
    return new Date(ts).toLocaleString("ko-KR", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

function BudgetBar({ items }: { items: ContextFile[] }) {
  const used = items.reduce((sum, f) => sum + (f.size ?? 0), 0);
  const pct = Math.min(100, (used / TOTAL_BUDGET_BYTES) * 100);
  const over = used > TOTAL_BUDGET_BYTES;
  const barColor = over
    ? "bg-rose-400"
    : pct > 80
      ? "bg-amber-400"
      : "bg-emerald-400";
  return (
    <div className="border border-zinc-800 bg-zinc-900/30 px-5 py-4">
      <div className="flex items-baseline justify-between mb-2.5">
        <span className="text-xs text-zinc-400 font-medium">총 사용량</span>
        <span
          className={`text-xs font-mono tabular-nums ${
            over ? "text-rose-400" : "text-zinc-300"
          }`}
        >
          {fmtBytes(used)}{" "}
          <span className="text-zinc-600">
            / {fmtBytes(TOTAL_BUDGET_BYTES)}
          </span>
        </span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-zinc-800 overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${barColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="mt-2 text-[11px] text-zinc-600 leading-relaxed">
        업로드된 파일 내용은 에이전트가 호출될 때 참조 데이터로 주입됩니다.
        명령(command)이 아닌 참고 정보로만 사용됩니다.
      </p>
    </div>
  );
}

// ── File row ─────────────────────────────────────────────────────────────────

function FileRow({
  file,
  onDelete,
  deleting,
}: {
  file: ContextFile;
  onDelete: () => void;
  deleting: boolean;
}) {
  const ts = fmtTs(file.updated_at);
  return (
    <div className="border-t border-zinc-800 first:border-t-0 px-5 py-4 flex flex-col sm:flex-row sm:items-start gap-3">
      <div className="flex-1 min-w-0">
        {/* Name + type pill */}
        <div className="flex items-center gap-2 flex-wrap mb-1.5">
          <span className="text-sm font-mono text-zinc-100 truncate max-w-[320px]">
            {file.name}
          </span>
          <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-zinc-800 border border-zinc-700 text-[10px] font-mono text-zinc-400 flex-shrink-0">
            {file.content_type}
          </span>
          <span className="text-[11px] font-mono text-zinc-500 flex-shrink-0">
            {fmtBytes(file.size)}
          </span>
        </div>
        {/* Provenance */}
        {(file.updated_by || ts) && (
          <div className="text-[11px] font-mono text-zinc-600">
            {file.updated_by && (
              <span className="text-zinc-500">{file.updated_by}</span>
            )}
            {file.updated_by && ts && (
              <span className="text-zinc-700"> · </span>
            )}
            {ts && <span>{ts}</span>}
          </div>
        )}
      </div>
      <button
        onClick={onDelete}
        disabled={deleting}
        className="flex-shrink-0 text-xs px-3 py-1.5 border border-zinc-800 text-zinc-600 hover:border-rose-500/50 hover:text-rose-400 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        aria-label={`${file.name} 삭제`}
      >
        {deleting ? "삭제 중…" : "삭제"}
      </button>
    </div>
  );
}

// ── Upload zone ───────────────────────────────────────────────────────────────

function UploadZone({
  onUpload,
  uploading,
  uploadError,
}: {
  onUpload: (file: File) => void;
  uploading: boolean;
  uploadError: string | null;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    onUpload(file);
    // Reset so the same file can be re-selected after an error
    e.target.value = "";
  };

  return (
    <div className="border border-zinc-800 bg-zinc-900/30">
      <div className="px-5 py-4">
        <p className="text-xs text-zinc-400 mb-3">
          <code className="text-zinc-300">.md</code>,{" "}
          <code className="text-zinc-300">.txt</code>,{" "}
          <code className="text-zinc-300">.csv</code> 형식만 허용 · 파일당 최대{" "}
          {fmtBytes(PER_FILE_MAX_BYTES)} · 전체 예산{" "}
          {fmtBytes(TOTAL_BUDGET_BYTES)}
        </p>
        <input
          ref={inputRef}
          type="file"
          accept=".md,.txt,.csv"
          onChange={handleChange}
          disabled={uploading}
          className="hidden"
          id="context-file-input"
          aria-label="컨텍스트 파일 선택"
        />
        <label
          htmlFor="context-file-input"
          className={`inline-flex items-center gap-2 text-xs font-medium px-5 py-2.5 cursor-pointer transition-colors ${
            uploading
              ? "bg-zinc-700 text-zinc-500 cursor-not-allowed"
              : "bg-emerald-400/90 text-zinc-950 hover:bg-emerald-300"
          }`}
          aria-disabled={uploading}
        >
          {uploading ? "업로드 중…" : "파일 선택 후 업로드"}
        </label>
      </div>

      {uploadError && (
        <div className="mx-5 mb-4 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs font-mono">
          {uploadError}
        </div>
      )}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function ContextFilesPage() {
  const [files, setFiles] = useState<ContextFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [adminOnly, setAdminOnly] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setLoadError(null);
    setAdminOnly(false);
    fetchContextFiles()
      .then((d) => setFiles(d.items))
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e);
        if (msg === "admin only") {
          setAdminOnly(true);
        } else {
          setLoadError(msg);
        }
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleUpload = async (file: File) => {
    setUploadError(null);

    // Client-side validation
    const ext = extOf(file.name);
    if (!ALLOWED_EXTENSIONS.has(ext)) {
      setUploadError(
        `.${ext} 형식은 지원하지 않습니다. .md, .txt, .csv 파일만 업로드할 수 있습니다.`,
      );
      return;
    }
    if (file.size > MAX_BYTES) {
      setUploadError(
        `파일 크기가 ${fmtBytes(file.size)}입니다. 파일당 최대 ${fmtBytes(
          MAX_BYTES,
        )}까지 업로드할 수 있습니다.`,
      );
      return;
    }

    let content: string;
    try {
      content = await file.text();
    } catch {
      setUploadError("파일을 읽는 중 오류가 발생했습니다.");
      return;
    }

    if (content.toLowerCase().includes(RESERVED_MARKER.toLowerCase())) {
      setUploadError(
        `파일에 예약어 "${RESERVED_MARKER}"가 포함되어 있어 업로드할 수 없습니다.`,
      );
      return;
    }

    setUploading(true);
    try {
      const created = await uploadContextFile({
        name: file.name,
        content,
        content_type: contentTypeOf(ext),
      });
      setFiles((prev) => [created, ...prev]);
    } catch (e: unknown) {
      setUploadError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (id: string, name: string) => {
    setDeleteError(null);
    if (
      !confirm(
        `"${name}" 파일을 삭제할까요? 에이전트 컨텍스트에서 즉시 제거됩니다.`,
      )
    )
      return;
    setDeletingId(id);
    try {
      await deleteContextFile(id);
      setFiles((prev) => prev.filter((f) => f.file_id !== id));
    } catch (e: unknown) {
      setDeleteError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeletingId(null);
    }
  };

  // ── Admin-only notice ─────────────────────────────────────────────────────

  if (!loading && adminOnly) {
    return (
      <PageBody>
        <PageHeader
          eyebrow="Configure"
          title="Context files"
          description="에이전트 참조 컨텍스트 파일 관리 (관리자 전용)"
        />
        <Section>
          <EmptyState
            eyebrow="접근 제한"
            title="관리자 전용 페이지"
            description="이 설정은 관리자만 변경할 수 있습니다."
          />
        </Section>
      </PageBody>
    );
  }

  return (
    <PageBody>
      <PageHeader
        eyebrow="Configure"
        title="Context files"
        description="에이전트가 작업할 때 참조하는 운영 컨텍스트 파일을 관리합니다. 업로드한 내용은 매 호출마다 에이전트에 참조 데이터로 주입되며, 명령(command)으로 해석되지 않습니다."
      />

      {/* ── How it works ── */}
      <Section eyebrow="동작 원리" title="컨텍스트 주입 방식">
        <div className="border border-zinc-800 bg-zinc-900/30 px-5 py-4 text-xs text-zinc-400 leading-relaxed space-y-2">
          <p>
            업로드된 파일은{" "}
            <strong className="text-zinc-300">에이전트 시스템 프롬프트</strong>{" "}
            뒤에 참조 섹션으로 삽입됩니다. 파일 내용은{" "}
            <strong className="text-zinc-300">명령이 아닌 참조 데이터</strong>
            로만 사용되며, 에이전트의 판단을 보조하는 용도입니다.
          </p>
          <p>
            예: 클러스터별 담당자 매핑, 점검 체크리스트, 내부 SLA 기준, 팀
            컨벤션 등을 <code className="text-zinc-400">.md</code> 파일로
            업로드하면 에이전트가 진단·권고 시 이를 참조합니다.
          </p>
          <p>
            파일당 최대{" "}
            <strong className="text-zinc-300">
              {fmtBytes(PER_FILE_MAX_BYTES)}
            </strong>
            , 전체 예산{" "}
            <strong className="text-zinc-300">
              {fmtBytes(TOTAL_BUDGET_BYTES)}
            </strong>
            . 예산 초과 시 업로드가 거부됩니다.
          </p>
        </div>
      </Section>

      {/* ── Load error ── */}
      {loadError && (
        <div className="mb-6 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs font-mono">
          {loadError}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-zinc-500">불러오는 중…</div>
      ) : (
        <>
          {/* ── Budget bar ── */}
          <Section eyebrow="Budget" title="예산 사용량">
            <BudgetBar items={files} />
          </Section>

          {/* ── Delete error ── */}
          {deleteError && (
            <div className="mb-6 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs font-mono">
              {deleteError}
            </div>
          )}

          {/* ── File list ── */}
          <Section
            eyebrow="Files"
            title="등록된 파일"
            description={`${files.length}개`}
          >
            {files.length === 0 ? (
              <EmptyState
                title="등록된 파일 없음"
                description="아래에서 첫 번째 컨텍스트 파일을 업로드하세요. 파일이 없으면 에이전트는 기본 참조 정보만 사용합니다."
              />
            ) : (
              <div className="border border-zinc-800 bg-zinc-900/30">
                {files.map((f) => (
                  <FileRow
                    key={f.file_id}
                    file={f}
                    onDelete={() => handleDelete(f.file_id, f.name)}
                    deleting={deletingId === f.file_id}
                  />
                ))}
              </div>
            )}
          </Section>

          {/* ── Upload ── */}
          <Section
            eyebrow="Upload"
            title="파일 업로드"
            description={`.md · .txt · .csv — 파일당 최대 ${fmtBytes(
              PER_FILE_MAX_BYTES,
            )}`}
          >
            <UploadZone
              onUpload={handleUpload}
              uploading={uploading}
              uploadError={uploadError}
            />
          </Section>
        </>
      )}
    </PageBody>
  );
}
