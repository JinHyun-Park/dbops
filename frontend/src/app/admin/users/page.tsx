"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchAdminUsers,
  updateUserRole,
  type AdminUser,
} from "@/lib/api-client";
import { getUsername } from "@/lib/auth";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";

export default function AdminUsersPage() {
  const [items, setItems] = useState<AdminUser[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [adminOnly, setAdminOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [me, setMe] = useState<string | null>(null);

  useEffect(() => {
    setMe(getUsername());
  }, []);

  const load = useCallback((cursor?: string) => {
    setLoading(true);
    setError(null);
    if (!cursor) setAdminOnly(false);
    fetchAdminUsers(cursor)
      .then((d) => {
        setItems((prev) => (cursor ? [...prev, ...d.items] : d.items));
        setNextCursor(d.next_cursor);
      })
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e);
        if (msg === "admin only") setAdminOnly(true);
        else setError(msg);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onChangeRole = async (u: AdminUser, role: "admin" | "viewer") => {
    if (role === u.role) return;
    const label = u.email || u.username;
    if (
      !window.confirm(
        `${label} 사용자의 역할을 '${role}'(으)로 변경하시겠습니까?`,
      )
    )
      return;
    setBusy(u.username);
    setError(null);
    try {
      await updateUserRole(u.username, role);
      setItems((prev) =>
        prev.map((it) =>
          it.username === u.username ? { ...it, role, implicit: false } : it,
        ),
      );
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  if (!loading && adminOnly) {
    return (
      <PageBody>
        <PageHeader
          eyebrow="Admin"
          title="Users"
          description="사용자 역할 관리 (관리자 전용)"
        />
        <Section>
          <EmptyState
            eyebrow="접근 제한"
            title="관리자 전용 페이지"
            description="이 페이지는 관리자만 볼 수 있습니다."
          />
        </Section>
      </PageBody>
    );
  }

  return (
    <PageBody>
      <PageHeader
        eyebrow="Admin"
        title="Users"
        description="사용자 목록과 역할(admin · viewer)을 관리합니다. 변경 사항은 즉시 적용됩니다."
      />

      {error && (
        <div className="mb-6 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {error}
        </div>
      )}

      {loading && items.length === 0 ? (
        <div className="text-sm text-zinc-500">불러오는 중…</div>
      ) : items.length === 0 ? (
        <Section>
          <EmptyState
            eyebrow="비어 있음"
            title="사용자가 없습니다"
            description="이 사용자 풀에 등록된 사용자가 없습니다."
          />
        </Section>
      ) : (
        <Section eyebrow="Identity" title="사용자">
          <div className="border border-zinc-800 bg-zinc-900/30">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-zinc-500 border-b border-zinc-800">
                  <th className="px-4 py-2.5 font-medium">Email</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5 font-medium">Role</th>
                  <th className="px-4 py-2.5 font-medium text-right">변경</th>
                </tr>
              </thead>
              <tbody>
                {items.map((u) => {
                  const isSelf = me != null && u.username === me;
                  return (
                    <tr
                      key={u.username}
                      className="border-b border-zinc-800/60 last:border-0"
                    >
                      <td className="px-4 py-3 text-zinc-200">
                        {u.email || (
                          <span className="font-mono text-zinc-500">
                            {u.username}
                          </span>
                        )}
                        {isSelf && (
                          <span className="ml-2 text-[10px] text-emerald-400/80">
                            (나)
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-zinc-400">
                        {u.status}
                        {!u.enabled && (
                          <span className="ml-1 text-rose-400">· 비활성</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={
                            u.role === "admin"
                              ? "text-emerald-300"
                              : "text-zinc-300"
                          }
                        >
                          {u.role}
                        </span>
                        {u.implicit && (
                          <span className="ml-2 text-[10px] text-zinc-500">
                            (암묵 — 명시 역할 미지정)
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <select
                          value={u.role}
                          disabled={isSelf || busy === u.username}
                          title={
                            isSelf
                              ? "자신의 역할은 변경할 수 없습니다"
                              : undefined
                          }
                          onChange={(e) =>
                            onChangeRole(
                              u,
                              e.target.value as "admin" | "viewer",
                            )
                          }
                          className="bg-zinc-800 border border-zinc-700 text-zinc-100 text-xs px-2 py-1 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                          <option value="admin">admin</option>
                          <option value="viewer">viewer</option>
                        </select>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {nextCursor && (
            <button
              type="button"
              onClick={() => load(nextCursor)}
              disabled={loading}
              className="mt-4 text-xs text-zinc-400 hover:text-zinc-200 border border-zinc-700 px-3 py-1.5 rounded disabled:opacity-40"
            >
              더 불러오기
            </button>
          )}
        </Section>
      )}
    </PageBody>
  );
}
