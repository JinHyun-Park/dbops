"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchAdminTeams,
  fetchTeamDetail,
  createTeam,
  deleteTeam,
  addTeamMember,
  removeTeamMember,
  assignClusterToTeam,
  unassignClusterFromTeam,
  fetchAdminUsers,
  fetchClusters,
  type AdminTeam,
  type TeamDetail,
  type AdminUser,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";

interface ClusterItem {
  cluster_id: string;
  team_id?: string;
  [key: string]: unknown;
}

export default function AdminTeamsPage() {
  const [teams, setTeams] = useState<AdminTeam[]>([]);
  const [loading, setLoading] = useState(true);
  const [adminOnly, setAdminOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [selected, setSelected] = useState<TeamDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [allUsers, setAllUsers] = useState<AdminUser[]>([]);
  const [allClusters, setAllClusters] = useState<ClusterItem[]>([]);

  const [newTeamName, setNewTeamName] = useState("");
  const [creating, setCreating] = useState(false);

  const [busy, setBusy] = useState(false);

  const selectedTeamId = useRef<string | null>(null);

  const loadTeams = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await fetchAdminTeams();
      setTeams(d.teams);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg === "admin only") setAdminOnly(true);
      else setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadTeams();
  }, [loadTeams]);

  useEffect(() => {
    fetchAdminUsers()
      .then((d) => setAllUsers(d.items))
      .catch((e: unknown) =>
        setError(
          `멤버 후보 조회 실패: ${e instanceof Error ? e.message : String(e)}`,
        ),
      );
    fetchClusters()
      .then((d: unknown) => {
        const items = Array.isArray(d)
          ? (d as ClusterItem[])
          : Array.isArray((d as { items?: unknown[] }).items)
            ? ((d as { items: ClusterItem[] }).items as ClusterItem[])
            : [];
        setAllClusters(items);
      })
      .catch((e: unknown) =>
        setError(
          `클러스터 목록 조회 실패: ${
            e instanceof Error ? e.message : String(e)
          }`,
        ),
      );
  }, []);

  const loadDetail = useCallback(async (teamId: string) => {
    setDetailLoading(true);
    setError(null);
    try {
      const d = await fetchTeamDetail(teamId);
      setSelected(d);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const refreshDetail = useCallback(async () => {
    if (!selectedTeamId.current) return;
    await loadDetail(selectedTeamId.current);
    await loadTeams();
  }, [loadDetail, loadTeams]);

  const onSelectTeam = (team: AdminTeam) => {
    selectedTeamId.current = team.team_id;
    loadDetail(team.team_id);
  };

  const onCreateTeam = async () => {
    const name = newTeamName.trim();
    if (!name) return;
    setCreating(true);
    setError(null);
    try {
      await createTeam(name);
      setNewTeamName("");
      await loadTeams();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  };

  const onDeleteTeam = async () => {
    if (!selected) return;
    if (
      !window.confirm(
        `팀 '${selected.name}'을(를) 삭제하시겠습니까? 해당 팀에 할당된 클러스터는 할당 해제됩니다.`,
      )
    )
      return;
    setBusy(true);
    setError(null);
    try {
      await deleteTeam(selected.team_id);
      selectedTeamId.current = null;
      setSelected(null);
      await loadTeams();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onRemoveMember = async (username: string) => {
    if (!selected) return;
    if (
      !window.confirm(
        `'${username}' 사용자를 팀 '${selected.name}'에서 제거하시겠습니까?`,
      )
    )
      return;
    setBusy(true);
    setError(null);
    try {
      await removeTeamMember(selected.team_id, username);
      await refreshDetail();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onAddMember = async (username: string) => {
    if (!selected || !username) return;
    setBusy(true);
    setError(null);
    try {
      await addTeamMember(selected.team_id, username);
      await refreshDetail();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onUnassignCluster = async (clusterId: string) => {
    if (!selected) return;
    if (
      !window.confirm(`클러스터 '${clusterId}'의 팀 할당을 해제하시겠습니까?`)
    )
      return;
    setBusy(true);
    setError(null);
    try {
      await unassignClusterFromTeam(selected.team_id, clusterId);
      await refreshDetail();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onAssignCluster = async (clusterId: string) => {
    if (!selected || !clusterId) return;
    setBusy(true);
    setError(null);
    try {
      await assignClusterToTeam(selected.team_id, clusterId);
      await refreshDetail();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Build a lookup from username → email for display
  const userEmailMap = Object.fromEntries(
    allUsers.map((u) => [u.username, u.email]),
  );

  // Members not yet in the selected team
  const nonMembers = selected
    ? allUsers.filter((u) => !selected.members.includes(u.username))
    : [];

  // Clusters not yet assigned to the selected team
  const unassignedClusters = selected
    ? allClusters.filter((c) => !selected.clusters.includes(c.cluster_id))
    : [];

  if (!loading && adminOnly) {
    return (
      <PageBody>
        <PageHeader
          eyebrow="Admin"
          title="Teams"
          description="팀 관리 — 멤버·클러스터 가시성 (관리자 전용)"
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
        title="Teams"
        description="팀을 만들고 멤버와 클러스터 가시성을 관리합니다."
      />

      {error && (
        <div className="mb-6 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {error}
        </div>
      )}

      {loading && teams.length === 0 ? (
        <div className="text-sm text-zinc-500">불러오는 중…</div>
      ) : (
        <Section eyebrow="Teams" title="팀 목록">
          {teams.length === 0 ? (
            <EmptyState
              eyebrow="비어 있음"
              title="팀이 없습니다"
              description="아래에서 첫 번째 팀을 만들어 보세요."
            />
          ) : (
            <div className="border border-zinc-800 bg-zinc-900/30">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-wide text-zinc-500 border-b border-zinc-800">
                    <th className="px-4 py-2.5 font-medium">이름</th>
                    <th className="px-4 py-2.5 font-medium">멤버 수</th>
                    <th className="px-4 py-2.5 font-medium text-right">관리</th>
                  </tr>
                </thead>
                <tbody>
                  {teams.map((t) => (
                    <tr
                      key={t.team_id}
                      className={`border-b border-zinc-800/60 last:border-0 cursor-pointer hover:bg-zinc-800/30 ${
                        selected?.team_id === t.team_id ? "bg-zinc-800/50" : ""
                      }`}
                      onClick={() => onSelectTeam(t)}
                    >
                      <td className="px-4 py-3 text-zinc-200">{t.name}</td>
                      <td className="px-4 py-3 text-zinc-400">
                        {t.member_count}명
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            onSelectTeam(t);
                          }}
                          className="text-xs text-zinc-400 hover:text-zinc-200 border border-zinc-700 px-2 py-1 rounded"
                        >
                          관리
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Create team */}
          <div className="mt-4 flex items-center gap-2">
            <input
              type="text"
              value={newTeamName}
              onChange={(e) => setNewTeamName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && onCreateTeam()}
              placeholder="새 팀 이름"
              className="bg-zinc-800 border border-zinc-700 text-zinc-100 text-xs px-3 py-1.5 rounded w-48 placeholder:text-zinc-500 focus:outline-none focus:border-zinc-500"
            />
            <button
              type="button"
              onClick={onCreateTeam}
              disabled={creating || !newTeamName.trim()}
              className="text-xs text-zinc-200 bg-zinc-700 hover:bg-zinc-600 border border-zinc-600 px-3 py-1.5 rounded disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {creating ? "생성 중…" : "팀 만들기"}
            </button>
          </div>
        </Section>
      )}

      {/* Team detail panel */}
      {selected && (
        <Section eyebrow="Team" title={selected.name}>
          {detailLoading ? (
            <div className="text-sm text-zinc-500">불러오는 중…</div>
          ) : (
            <div className="space-y-6">
              {/* Members */}
              <div>
                <h3 className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
                  멤버
                </h3>
                {selected.members.length === 0 ? (
                  <p className="text-xs text-zinc-500">
                    이 팀에 멤버가 없습니다.
                  </p>
                ) : (
                  <div className="border border-zinc-800 bg-zinc-900/30">
                    <table className="w-full text-sm">
                      <tbody>
                        {selected.members.map((username) => {
                          const email = userEmailMap[username];
                          return (
                            <tr
                              key={username}
                              className="border-b border-zinc-800/60 last:border-0"
                            >
                              <td className="px-4 py-3 text-zinc-200">
                                {email || (
                                  <span className="font-mono text-zinc-400">
                                    {username}
                                  </span>
                                )}
                              </td>
                              <td className="px-4 py-3 text-right">
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => onRemoveMember(username)}
                                  className="text-xs text-rose-400 hover:text-rose-300 border border-rose-800/60 px-2 py-1 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                                >
                                  제거
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}

                {/* Add member */}
                {nonMembers.length > 0 && (
                  <div className="mt-3 flex items-center gap-2">
                    <select
                      defaultValue=""
                      disabled={busy}
                      onChange={(e) => {
                        if (e.target.value) onAddMember(e.target.value);
                        e.target.value = "";
                      }}
                      className="bg-zinc-800 border border-zinc-700 text-zinc-100 text-xs px-2 py-1.5 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      <option value="" disabled>
                        멤버 추가…
                      </option>
                      {nonMembers.map((u) => (
                        <option key={u.username} value={u.username}>
                          {u.email || u.username}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
              </div>

              {/* Clusters */}
              <div>
                <h3 className="text-[11px] uppercase tracking-wide text-zinc-500 mb-2">
                  클러스터
                </h3>
                {selected.clusters.length === 0 ? (
                  <p className="text-xs text-zinc-500">
                    이 팀에 할당된 클러스터가 없습니다.
                  </p>
                ) : (
                  <div className="border border-zinc-800 bg-zinc-900/30">
                    <table className="w-full text-sm">
                      <tbody>
                        {selected.clusters.map((clusterId) => (
                          <tr
                            key={clusterId}
                            className="border-b border-zinc-800/60 last:border-0"
                          >
                            <td className="px-4 py-3 font-mono text-zinc-300 text-xs">
                              {clusterId}
                            </td>
                            <td className="px-4 py-3 text-right">
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => onUnassignCluster(clusterId)}
                                className="text-xs text-rose-400 hover:text-rose-300 border border-rose-800/60 px-2 py-1 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                              >
                                할당 해제
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {/* Assign cluster */}
                {unassignedClusters.length > 0 && (
                  <div className="mt-3 flex items-center gap-2">
                    <select
                      defaultValue=""
                      disabled={busy}
                      onChange={(e) => {
                        if (e.target.value) onAssignCluster(e.target.value);
                        e.target.value = "";
                      }}
                      className="bg-zinc-800 border border-zinc-700 text-zinc-100 text-xs px-2 py-1.5 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      <option value="" disabled>
                        클러스터 할당…
                      </option>
                      {unassignedClusters.map((c) => (
                        <option key={c.cluster_id} value={c.cluster_id}>
                          {c.cluster_id}
                          {c.team_id ? ` (현재: ${c.team_id})` : ""}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
              </div>

              {/* Delete team */}
              <div className="pt-2 border-t border-zinc-800/60">
                <button
                  type="button"
                  disabled={busy}
                  onClick={onDeleteTeam}
                  className="text-xs text-rose-400 hover:text-rose-300 border border-rose-800/60 px-3 py-1.5 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  팀 삭제
                </button>
              </div>
            </div>
          )}
        </Section>
      )}
    </PageBody>
  );
}
