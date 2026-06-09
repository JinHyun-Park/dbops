// Shared root-cause-analysis prompt. Used by the in-dashboard RCA side panel
// (streams via the agent SSE, see rca-drawer.tsx) AND by the optional "continue
// in chat" deep-link below — so both surfaces ask the agent the same thing and
// the UI never duplicates the diagnose_root_cause scoring (which lives in the
// incident MCP server).
export const RCA_PROMPT =
  "지금 이 클러스터에 무슨 일이 일어나고 있는지 근본 원인을 분석해줘. " +
  "최근 신호(이상치, blocking lock, RDS 이벤트, 느려진 쿼리)를 시간순으로 " +
  "상관분석해서 가장 가능성 높은 원인부터 정리하고, 다음 확인할 것과 권장 " +
  "조치를 알려줘.";

// Deep-link into the full chat with the RCA prompt + cluster pre-filled — the
// "continue this as a conversation" escape hatch from the side panel.
export function rcaChatHref(clusterId: string | null | undefined): string {
  const params = new URLSearchParams();
  if (clusterId) params.set("cluster", clusterId);
  params.set("prompt", RCA_PROMPT);
  return `/chat?${params.toString()}`;
}
