// One-shot handoff from the RCA side panel to the chat: when the user clicks
// "전체 대화로 이어가기", we carry the question AND the already-streamed analysis
// into a brand-new chat conversation, instead of dropping a bare prompt into
// whatever thread happened to be open (which discarded the analysis). Ephemeral
// (sessionStorage, same tab) and consumed once.

export interface RcaHandoff {
  cluster_id: string;
  prompt: string;
  analysis: string;
  tools: { name: string; status: string }[];
}

const KEY = "dbops_rca_handoff";

export function stashRcaHandoff(h: RcaHandoff): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.setItem(KEY, JSON.stringify(h));
  } catch {
    /* ignore — handoff is best-effort */
  }
}

// Read + clear in one call so a refresh can't replay it.
export function takeRcaHandoff(): RcaHandoff | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    sessionStorage.removeItem(KEY);
    return JSON.parse(raw) as RcaHandoff;
  } catch {
    return null;
  }
}
