// Shared root-cause-analysis prompt. Used by the in-dashboard RCA side panel
// (streams via the agent SSE, see rca-drawer.tsx) AND by the optional "continue
// in chat" deep-link below — so both surfaces ask the agent the same thing and
// the UI never duplicates the diagnose_root_cause scoring (which lives in the
// incident MCP server).
export const RCA_PROMPT =
  "이 클러스터의 근본 원인 분석을 수행해줘. 일반론적인 가이드가 아니라, " +
  "반드시 사용 가능한 진단 도구(diagnose_root_cause, correlate_signals, " +
  "이상치/blocking lock/최근 이벤트/느린 쿼리 조회 등)를 직접 호출해서 " +
  "이 클러스터의 실제 데이터로 분석해줘. 수집된 신호를 시간순으로 상관분석해 " +
  "가장 가능성 높은 원인부터 근거(실제 수치)와 함께 정리하고, 다음 확인할 것과 " +
  "권장 조치를 제시해줘. 데이터가 부족하면 어떤 데이터가 없는지 명시해줘.";

// Deep-link into the full chat with the RCA prompt + cluster pre-filled — the
// "continue this as a conversation" escape hatch from the side panel.
export function rcaChatHref(clusterId: string | null | undefined): string {
  const params = new URLSearchParams();
  if (clusterId) params.set("cluster", clusterId);
  params.set("prompt", RCA_PROMPT);
  return `/chat?${params.toString()}`;
}
