// Deep-link into the AI chat with a cluster-scoped root-cause-analysis prompt
// pre-filled. The agent answers via its diagnose_root_cause / correlate_signals
// MCP tools, so the UI never duplicates the RCA scoring logic (which lives in
// the incident MCP server) and can't drift from it. The chat panel reads
// ?cluster= and ?prompt= on mount (see chat-panel.tsx).
export function rcaChatHref(clusterId: string | null | undefined): string {
  const prompt =
    "지금 이 클러스터에 무슨 일이 일어나고 있는지 근본 원인을 분석해줘. " +
    "최근 신호(이상치, blocking lock, RDS 이벤트, 느려진 쿼리)를 시간순으로 " +
    "상관분석해서 가장 가능성 높은 원인부터 정리하고, 다음 확인할 것과 권장 " +
    "조치를 알려줘.";
  const params = new URLSearchParams();
  if (clusterId) params.set("cluster", clusterId);
  params.set("prompt", prompt);
  return `/chat?${params.toString()}`;
}
