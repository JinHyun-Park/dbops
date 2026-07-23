"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchApprovalPolicies,
  createApprovalPolicy,
  updateApprovalPolicy,
  deleteApprovalPolicy,
  type ApprovalPolicy,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";

// ── Helpers ─────────────────────────────────────────────────────────────────

function parseApprovers(raw: string): string[] {
  return raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
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

// ── Inline provenance ────────────────────────────────────────────────────────

function Provenance({ policy }: { policy: ApprovalPolicy }) {
  const ts = fmtTs(policy.updated_at);
  if (!policy.updated_by && !ts) return null;
  return (
    <div className="mt-1 text-[11px] font-mono text-zinc-600">
      {policy.updated_by && (
        <span className="text-zinc-500">{policy.updated_by}</span>
      )}
      {policy.updated_by && ts && <span className="text-zinc-700"> · </span>}
      {ts && <span className="text-zinc-500">{ts}</span>}
    </div>
  );
}

// ── Policy form (add or edit) ────────────────────────────────────────────────

interface PolicyFormState {
  cluster_id: string;
  action_type: string;
  approvers: string; // raw textarea value
  description: string;
}

const EMPTY_FORM: PolicyFormState = {
  cluster_id: "*",
  action_type: "*",
  approvers: "",
  description: "",
};

function policyToForm(p: ApprovalPolicy): PolicyFormState {
  return {
    cluster_id: p.cluster_id,
    action_type: p.action_type,
    approvers: p.approvers.join("\n"),
    description: p.description,
  };
}

interface PolicyFormProps {
  initial?: PolicyFormState;
  resetKey: string;
  submitting: boolean;
  error: string | null;
  submitLabel: string;
  onSubmit: (form: PolicyFormState) => void;
  onCancel?: () => void;
}

function PolicyForm({
  initial = EMPTY_FORM,
  resetKey,
  submitting,
  error,
  submitLabel,
  onSubmit,
  onCancel,
}: PolicyFormProps) {
  const [form, setForm] = useState<PolicyFormState>(initial);

  // Sync initial only when the edit target changes, not on every new object ref
  useEffect(() => {
    setForm(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  const set =
    (k: keyof PolicyFormState) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      setForm((prev) => ({ ...prev, [k]: e.target.value }));

  const inputCls =
    "w-full bg-zinc-950 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-emerald-500/60 disabled:opacity-40 font-mono placeholder:text-zinc-600";

  return (
    <div className="border border-zinc-800 bg-zinc-900/30">
      <div className="px-5 py-4 grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* cluster_id */}
        <div>
          <label className="block text-xs text-zinc-400 mb-1.5 font-medium">
            cluster_id
          </label>
          <input
            type="text"
            value={form.cluster_id}
            onChange={set("cluster_id")}
            placeholder="* (전체) 또는 특정 cluster id"
            disabled={submitting}
            className={inputCls}
            aria-label="cluster_id"
          />
          <p className="mt-1 text-[11px] text-zinc-600">
            <code className="text-zinc-500">*</code> = 모든 클러스터에 적용
          </p>
        </div>

        {/* action_type */}
        <div>
          <label className="block text-xs text-zinc-400 mb-1.5 font-medium">
            action_type
          </label>
          <input
            type="text"
            value={form.action_type}
            onChange={set("action_type")}
            placeholder="* 또는 execute_sql, create_custom_endpoint, add_reader_instance …"
            disabled={submitting}
            className={inputCls}
            aria-label="action_type"
            list="action-type-options"
          />
          <datalist id="action-type-options">
            <option value="*" />
            {/* SQL / 파라미터 */}
            <option value="execute_sql" />
            <option value="modify_parameter" />
            {/* 스케일 / 엔드포인트 */}
            <option value="modify_scaling" />
            <option value="create_custom_endpoint" />
            <option value="delete_custom_endpoint" />
            <option value="modify_custom_endpoint" />
            <option value="prewarm_reader" />
            <option value="add_reader_instance" />
            <option value="remove_reader_instance" />
            <option value="scale_out_with_warmup" />
            {/* RDS 인스턴스(비-Aurora MySQL/SQL Server) write */}
            <option value="reboot_rds_instance" />
            <option value="create_rds_snapshot" />
            <option value="modify_rds_instance_class" />
            {/* 유지보수 / 백업·복원 */}
            <option value="manage_maintenance" />
            <option value="create_snapshot" />
            <option value="restore_cluster" />
            <option value="enable_data_api" />
            {/* NoSQL / 캐시 */}
            <option value="modify_dynamodb_capacity" />
            <option value="modify_dynamodb_ttl" />
            <option value="enable_dynamodb_pitr" />
            <option value="set_docdb_profiler" />
            <option value="create_docdb_index" />
            <option value="modify_elasticache_node_type" />
            <option value="create_elasticache_snapshot" />
            <option value="reboot_elasticache" />
            <option value="test_elasticache_failover" />
            <option value="other" />
          </datalist>
          <p className="mt-1 text-[11px] text-zinc-600">
            승인 요청의 action_type / tool_name과 매칭 — SQL·파라미터뿐 아니라
            엔드포인트·스케일 변경(create_custom_endpoint, add_reader_instance
            등)도 지정 가능. 값이 요청의 action_type과{" "}
            <strong className="text-zinc-400">정확히 일치</strong>해야
            적용됩니다 (오타 시 정책이 매칭되지 않아 미승인 상태로 남음).
          </p>
        </div>

        {/* approvers */}
        <div className="md:col-span-2">
          <label className="block text-xs text-zinc-400 mb-1.5 font-medium">
            approvers
          </label>
          <textarea
            value={form.approvers}
            onChange={set("approvers")}
            placeholder={"admin@example.com\ndba@example.com"}
            disabled={submitting}
            rows={3}
            className={`${inputCls} resize-y`}
            aria-label="approvers"
          />
          <p className="mt-1 text-[11px] text-zinc-600">
            쉼표 또는 줄바꿈으로 구분 — 최소 1명 필수
          </p>
        </div>

        {/* description */}
        <div className="md:col-span-2">
          <label className="block text-xs text-zinc-400 mb-1.5 font-medium">
            description
          </label>
          <input
            type="text"
            value={form.description}
            onChange={set("description")}
            placeholder="정책 설명 (선택)"
            disabled={submitting}
            className={inputCls}
            aria-label="description"
          />
        </div>
      </div>

      {error && (
        <div className="mx-5 mb-4 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs font-mono">
          {error}
        </div>
      )}

      <div className="px-5 pb-4 flex items-center gap-3">
        <button
          onClick={() => onSubmit(form)}
          disabled={submitting}
          className="text-xs font-medium px-5 py-2.5 bg-emerald-400/90 text-zinc-950 hover:bg-emerald-300 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {submitting ? "저장 중…" : submitLabel}
        </button>
        {onCancel && (
          <button
            onClick={onCancel}
            disabled={submitting}
            className="text-xs font-medium px-4 py-2.5 border border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 transition-colors disabled:opacity-40"
          >
            취소
          </button>
        )}
      </div>
    </div>
  );
}

// ── Policy row ───────────────────────────────────────────────────────────────

interface PolicyRowProps {
  policy: ApprovalPolicy;
  editing: boolean;
  onEdit: () => void;
  onSave: (form: PolicyFormState) => void;
  onCancelEdit: () => void;
  onDelete: () => void;
  saving: boolean;
  editError: string | null;
}

function PolicyRow({
  policy,
  editing,
  onEdit,
  onSave,
  onCancelEdit,
  onDelete,
  saving,
  editError,
}: PolicyRowProps) {
  if (editing) {
    return (
      <div className="border-t border-zinc-800 first:border-t-0">
        <div className="px-5 pt-4 pb-1 text-xs text-zinc-500 font-mono">
          수정 중: {policy.policy_id}
        </div>
        <PolicyForm
          initial={policyToForm(policy)}
          resetKey={policy.policy_id}
          submitting={saving}
          error={editError}
          submitLabel="수정 저장"
          onSubmit={onSave}
          onCancel={onCancelEdit}
        />
      </div>
    );
  }

  return (
    <div className="border-t border-zinc-800 first:border-t-0 px-5 py-4 flex flex-col sm:flex-row sm:items-start gap-3">
      <div className="flex-1 min-w-0 grid grid-cols-1 sm:grid-cols-[auto_auto_1fr] gap-x-6 gap-y-1 text-sm">
        {/* Scope pills */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-zinc-800 border border-zinc-700 text-xs font-mono text-zinc-300">
            <span className="text-zinc-500">cluster</span>{" "}
            {policy.cluster_id === "*" ? (
              <span className="text-emerald-300">*</span>
            ) : (
              <span className="text-zinc-100">{policy.cluster_id}</span>
            )}
          </span>
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-zinc-800 border border-zinc-700 text-xs font-mono text-zinc-300">
            <span className="text-zinc-500">action</span>{" "}
            {policy.action_type === "*" ? (
              <span className="text-emerald-300">*</span>
            ) : (
              <span className="text-zinc-100">{policy.action_type}</span>
            )}
          </span>
        </div>

        {/* Approvers */}
        <div className="sm:col-start-1">
          <div className="text-xs text-zinc-500 mb-0.5">승인자</div>
          <div className="font-mono text-xs text-zinc-300 leading-relaxed">
            {policy.approvers.length === 0 ? (
              <span className="text-rose-400">없음</span>
            ) : (
              policy.approvers.join(", ")
            )}
          </div>
        </div>

        {/* Description + provenance */}
        {(policy.description || policy.updated_by || policy.updated_at) && (
          <div className="sm:col-start-1">
            {policy.description && (
              <div className="text-xs text-zinc-500">{policy.description}</div>
            )}
            <Provenance policy={policy} />
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 flex-shrink-0">
        <button
          onClick={onEdit}
          className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
        >
          수정
        </button>
        <button
          onClick={onDelete}
          className="text-xs px-3 py-1.5 border border-zinc-800 text-zinc-600 hover:border-rose-500/50 hover:text-rose-400 transition-colors"
        >
          삭제
        </button>
      </div>
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function ApprovalPoliciesPage() {
  const [policies, setPolicies] = useState<ApprovalPolicy[]>([]);
  const [loading, setLoading] = useState(true);
  const [adminOnly, setAdminOnly] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Add form state
  const [addSubmitting, setAddSubmitting] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  // Edit state (one row at a time)
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setLoadError(null);
    setAdminOnly(false);
    fetchApprovalPolicies()
      .then((d) => setPolicies(d.policies))
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

  const handleAdd = async (form: PolicyFormState) => {
    setAddSubmitting(true);
    setAddError(null);
    try {
      const created = await createApprovalPolicy({
        cluster_id: form.cluster_id.trim() || "*",
        action_type: form.action_type.trim() || "*",
        approvers: parseApprovers(form.approvers),
        description: form.description.trim(),
      });
      setPolicies((prev) => [created, ...prev]);
    } catch (e: unknown) {
      setAddError(e instanceof Error ? e.message : String(e));
    } finally {
      setAddSubmitting(false);
    }
  };

  const handleEdit = async (id: string, form: PolicyFormState) => {
    setEditSubmitting(true);
    setEditError(null);
    try {
      const updated = await updateApprovalPolicy(id, {
        cluster_id: form.cluster_id.trim() || "*",
        action_type: form.action_type.trim() || "*",
        approvers: parseApprovers(form.approvers),
        description: form.description.trim(),
      });
      setPolicies((prev) =>
        prev.map((p) => (p.policy_id === id ? updated : p)),
      );
      setEditingId(null);
    } catch (e: unknown) {
      setEditError(e instanceof Error ? e.message : String(e));
    } finally {
      setEditSubmitting(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (
      !confirm(
        "이 정책을 삭제하면 해당 조건의 승인 요청이 기본 승인(모든 관리자)으로 fallback됩니다. 계속할까요?",
      )
    )
      return;
    setDeleteError(null);
    try {
      await deleteApprovalPolicy(id);
      setPolicies((prev) => prev.filter((p) => p.policy_id !== id));
    } catch (e: unknown) {
      setDeleteError(e instanceof Error ? e.message : String(e));
    }
  };

  // ── Admin-only notice ───────────────────────────────────────────────────

  if (!loading && adminOnly) {
    return (
      <PageBody>
        <PageHeader
          eyebrow="Configure"
          title="Approval policies"
          description="클러스터·액션별 지정 승인자 라우팅 (관리자 전용)"
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
        title="Approval policies"
        description="클러스터·액션 타입별로 지정 승인자를 라우팅합니다. 매칭된 정책이 있으면 목록에 없는 관리자는 승인 불가 — 미매칭 요청은 모든 관리자에게 fallback."
      />

      {/* ── How it works ── */}
      <Section eyebrow="동작 원리" title="정책 매칭 규칙">
        <div className="border border-zinc-800 bg-zinc-900/30 px-5 py-4 text-xs text-zinc-400 leading-relaxed space-y-2">
          <p>
            <code className="text-emerald-300/80">*</code> 와일드카드는 모든
            cluster_id 또는 action_type에 매칭됩니다.{" "}
            <strong className="text-zinc-300">most-specific-wins</strong> —
            cluster_id + action_type 둘 다 구체적인 정책이 우선 적용됩니다.
          </p>
          <p>
            정책이 매칭되면{" "}
            <strong className="text-zinc-300">
              목록에 없는 관리자는 승인할 수 없습니다
            </strong>{" "}
            (명시된 승인자만 가능). 매칭되는 정책이 없으면 기본 동작(모든 관리자
            승인 가능)으로 fallback.
          </p>
          <p>
            <code className="text-zinc-400">action_type</code>은 승인 요청의
            action_type / tool_name과 비교합니다 — 예:{" "}
            <code className="text-zinc-400">execute_sql</code>,{" "}
            <code className="text-zinc-400">modify_parameter</code>,{" "}
            <code className="text-zinc-400">create_snapshot</code>.
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
          {/* ── Delete error ── */}
          {deleteError && (
            <div className="mb-6 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs font-mono">
              {deleteError}
            </div>
          )}

          {/* ── Existing policies ── */}
          <Section
            eyebrow="Policies"
            title="등록된 정책"
            description={`${policies.length}개`}
          >
            {policies.length === 0 ? (
              <EmptyState
                title="등록된 정책 없음"
                description="아래 폼에서 첫 번째 정책을 추가하세요. 정책이 없으면 모든 관리자가 모든 승인 요청을 처리할 수 있습니다."
              />
            ) : (
              <div className="border border-zinc-800 bg-zinc-900/30">
                {policies.map((p) => (
                  <PolicyRow
                    key={p.policy_id}
                    policy={p}
                    editing={editingId === p.policy_id}
                    onEdit={() => {
                      setEditingId(p.policy_id);
                      setEditError(null);
                    }}
                    onSave={(form) => handleEdit(p.policy_id, form)}
                    onCancelEdit={() => {
                      setEditingId(null);
                      setEditError(null);
                    }}
                    onDelete={() => handleDelete(p.policy_id)}
                    saving={editSubmitting && editingId === p.policy_id}
                    editError={editingId === p.policy_id ? editError : null}
                  />
                ))}
              </div>
            )}
          </Section>

          {/* ── Add new policy ── */}
          <Section
            eyebrow="Add"
            title="정책 추가"
            description="같은 cluster_id + action_type 조합이 여러 개면, 매칭 시 승인자 목록이 합산됩니다."
          >
            <PolicyForm
              initial={EMPTY_FORM}
              resetKey="__add__"
              submitting={addSubmitting}
              error={addError}
              submitLabel="정책 추가"
              onSubmit={handleAdd}
            />
          </Section>
        </>
      )}
    </PageBody>
  );
}
