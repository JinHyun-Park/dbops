/**
 * useAlertBadge — global fleet-health alert badge state.
 *
 * Data source: reuses fetchMultiClusterOverview() + triage() — the exact same
 * fetch the Fleet page performs, so no new endpoint needed. Derives a count
 * of clusters currently at "critical" or "warning" from the overview rows.
 *
 * Net-new logic:
 *  - The "seen baseline" is the last criticalCount+warningCount snapshot that
 *    the user has explicitly dismissed (bell click) OR the count at first load.
 *    It is persisted in localStorage so a refresh doesn't re-toast.
 *  - A toast fires only when the current count EXCEEDS the seen baseline, i.e.
 *    something genuinely new appeared since the user last acknowledged.
 *  - Toast text: "⚠ <cluster> critical: <reason>" for the first newly-critical
 *    cluster. If multiple clusters tipped critical simultaneously the toast
 *    summarises ("N개 클러스터 상태 악화").
 *
 * The hook returns:
 *  { criticalCount, warningCount, toasts, dismissToast, markSeen }
 *
 *  - criticalCount / warningCount: current badge numbers
 *  - toasts: active toast messages (auto-expire after TOAST_TTL_MS)
 *  - dismissToast(id): manually close a toast
 *  - markSeen(): called when the user clicks the bell; advances the baseline
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchMultiClusterOverview } from "@/lib/api-client";
import { triage, type TriageInput } from "@/lib/cluster-triage";
import { useSmartPoll } from "@/lib/use-smart-poll";
import { subscribeAlertStream } from "@/lib/alert-stream";

const SEEN_KEY = "dbops_alert_badge_seen_v1";
const TOAST_TTL_MS = 6000;
// Poll the global overview at this cadence (active tab only).
const BADGE_POLL_MS = 45_000;

export interface AlertToast {
  id: string;
  message: string;
  severity: "critical" | "warning";
  /** cluster_id, if known — used by the toast to deep-link to /dashboard?cluster= */
  cluster_id?: string;
}

export interface AlertBadgeState {
  criticalCount: number;
  warningCount: number;
  toasts: AlertToast[];
  dismissToast: (id: string) => void;
  markSeen: () => void;
}

// Minimal shape returned by /api/multi-cluster/overview
interface OverviewCluster {
  cluster_id: string;
  resource_name?: string;
  cpu?: number | string | null;
  aas?: number | string | null;
  deadlocks?: number | string | null;
  blocking_count?: number | string | null;
  status?: string | null;
}

function loadSeenCount(): number {
  try {
    const raw = localStorage.getItem(SEEN_KEY);
    return raw ? parseInt(raw, 10) : -1; // -1 = "first load, no baseline yet"
  } catch {
    return -1;
  }
}

function persistSeenCount(n: number): void {
  try {
    localStorage.setItem(SEEN_KEY, String(n));
  } catch {
    // private mode / quota — ephemeral only
  }
}

export function useAlertBadge(): AlertBadgeState {
  const [criticalCount, setCriticalCount] = useState(0);
  const [warningCount, setWarningCount] = useState(0);
  const [toasts, setToasts] = useState<AlertToast[]>([]);

  // Previous triaged state — used to detect net-new clusters tipping into
  // critical or warning since the last poll.
  const prevLevels = useRef<Map<string, "critical" | "warning" | "ok">>(
    new Map(),
  );
  const seenBaseline = useRef<number>(-1);
  const initialized = useRef(false);

  useEffect(() => {
    seenBaseline.current = loadSeenCount();
  }, []);

  const addToast = useCallback(
    (msg: string, severity: "critical" | "warning", cluster_id?: string) => {
      const id = `toast-${Date.now()}-${Math.random()
        .toString(36)
        .slice(2, 6)}`;
      setToasts((prev) => [
        ...prev,
        { id, message: msg, severity, cluster_id },
      ]);
      // Auto-dismiss after TTL.
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, TOAST_TTL_MS);
    },
    [],
  );

  const dismissToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const markSeen = useCallback(() => {
    const total = criticalCount + warningCount;
    seenBaseline.current = total;
    persistSeenCount(total);
  }, [criticalCount, warningCount]);

  const poll = useCallback(() => {
    fetchMultiClusterOverview()
      .then((data) => {
        const clusters: OverviewCluster[] = data?.clusters ?? [];
        let crit = 0;
        let warn = 0;
        const newCritical: OverviewCluster[] = [];

        const nextLevels = new Map<string, "critical" | "warning" | "ok">();

        for (const c of clusters) {
          const input: TriageInput = {
            status: c.status,
            cpu: c.cpu,
            aas: c.aas,
            deadlocks: c.deadlocks,
            blocking_count: c.blocking_count,
          };
          const { level } = triage(input, null);
          nextLevels.set(c.cluster_id, level);

          if (level === "critical") {
            crit++;
            // A cluster is "newly critical" if it wasn't critical last poll.
            const prev = prevLevels.current.get(c.cluster_id);
            if (initialized.current && prev !== "critical") {
              newCritical.push(c);
            }
          } else if (level === "warning") {
            warn++;
          }
        }

        prevLevels.current = nextLevels;

        setCriticalCount(crit);
        setWarningCount(warn);

        const total = crit + warn;

        if (!initialized.current) {
          // First load: establish baseline if none stored.
          initialized.current = true;
          if (seenBaseline.current === -1) {
            seenBaseline.current = total;
            persistSeenCount(total);
          }
          return;
        }

        // Fire toasts only when count INCREASES beyond baseline.
        if (total > seenBaseline.current) {
          if (newCritical.length === 1) {
            const c = newCritical[0];
            const name = c.resource_name || c.cluster_id;
            addToast(`${name} critical 전환`, "critical", c.cluster_id);
          } else if (newCritical.length > 1) {
            addToast(
              `${newCritical.length}개 클러스터 critical 전환`,
              "critical",
              // multiple clusters — no single deep-link target
            );
          } else if (crit + warn > seenBaseline.current) {
            addToast(
              `경보 증가: critical ${crit} / warning ${warn}`,
              "warning",
            );
          }
        }
      })
      .catch(() => {
        // Silent failure — badge simply stays at last known values.
      });
  }, [addToast]);

  useSmartPoll(poll, BADGE_POLL_MS);

  // Real-time push: fired alerts / external incidents toast INSTANTLY instead
  // of waiting for the next 45s badge poll. Additive — the poll still drives the
  // badge counts; the WS just delivers the toast the moment an alert fires.
  useEffect(() => {
    const unsub = subscribeAlertStream((a) => {
      const severity = a.severity === "critical" ? "critical" : "warning";
      const label = a.cluster_id ? `${a.cluster_id}: ` : "";
      const fallback = a.type === "incident" ? "외부 인시던트" : "새 경보";
      addToast(`${label}${a.title || fallback}`, severity, a.cluster_id);
    });
    return unsub;
  }, [addToast]);

  return { criticalCount, warningCount, toasts, dismissToast, markSeen };
}
