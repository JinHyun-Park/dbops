/**
 * alert-stream — singleton WebSocket client for the in-app alert push channel.
 *
 * Connects to the API Gateway WebSocket (config.json `webSocketUrl`), passing
 * the Cognito ACCESS token as `?token=` (browsers can't set WS headers; the
 * $connect Lambda authorizer validates it via Cognito GetUser). Pushed alerts /
 * incidents are fanned out to subscribers. Reconnects with exponential backoff.
 *
 * Audience model: the push is FLEET-WIDE — every connected operator receives
 * every fired alert/incident. This is deliberately consistent with the REST
 * layer, which already serves fleet-wide alert metadata to any authenticated
 * user (cognito:groups gate WRITE actions, not which clusters you can SEE). The
 * WS channel therefore exposes nothing beyond the existing 45s poll. ("Scoped"
 * in the feature name means a scoped *slice* of the SSE-push backlog item, not
 * per-user scoping.) If per-cluster RBAC is ever added, the REST layer AND this
 * broadcast must grow audience filtering together — it is not a WS-only concern.
 *
 * Additive to polling: if the channel isn't configured or the socket drops, the
 * existing 45s badge poll still covers fleet health — nothing breaks.
 *
 * Auth lifecycle: opens on the first subscriber once logged in; tears down on
 * `dbops:auth-logout` (so a logged-out user stops receiving pushes instead of
 * riding the socket to its 2h TTL) and re-opens on `dbops:auth-login`.
 */
import { getValidAccessToken, isLoggedIn } from "@/lib/auth";

export interface PushedAlert {
  type: "alert" | "incident";
  source?: string;
  cluster_id?: string;
  severity?: string;
  title?: string;
}

let wsUrlPromise: Promise<string> | null = null;
function loadWsUrl(): Promise<string> {
  if (wsUrlPromise) return wsUrlPromise;
  wsUrlPromise = (async () => {
    if (typeof window === "undefined") return "";
    try {
      const res = await fetch("/config.json", { cache: "no-store" });
      if (res.ok) {
        const cfg = await res.json();
        return cfg.webSocketUrl || "";
      }
    } catch {
      // fall through
    }
    return "";
  })();
  return wsUrlPromise;
}

type Listener = (a: PushedAlert) => void;
const listeners = new Set<Listener>();
let socket: WebSocket | null = null;
let backoff = 1000;
let started = false;
let closedByUs = false;
// Suspended between logout and the next login: blocks both connect() and the
// reconnect timer so a logged-out tab doesn't spin retrying with no token.
let suspended = false;

async function connect(): Promise<void> {
  if (typeof window === "undefined" || socket || suspended) return;
  const base = await loadWsUrl();
  if (!base) return; // push channel not configured on this deployment
  if (!isLoggedIn()) {
    scheduleReconnect(); // not logged in yet — retry later
    return;
  }
  const token = await getValidAccessToken();
  if (!token) {
    scheduleReconnect();
    return;
  }
  closedByUs = false;
  try {
    socket = new WebSocket(`${base}?token=${encodeURIComponent(token)}`);
  } catch {
    scheduleReconnect();
    return;
  }
  socket.onopen = () => {
    backoff = 1000;
  };
  socket.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data) as PushedAlert;
      listeners.forEach((l) => {
        try {
          l(data);
        } catch {
          // a bad listener must not break the others
        }
      });
    } catch {
      // ignore malformed frames
    }
  };
  socket.onclose = () => {
    socket = null;
    if (!closedByUs) scheduleReconnect();
  };
  socket.onerror = () => {
    try {
      socket?.close();
    } catch {
      // ignore
    }
  };
}

function scheduleReconnect(): void {
  const wait = Math.min(backoff, 30_000);
  backoff = Math.min(backoff * 2, 30_000);
  window.setTimeout(() => {
    if (!suspended && listeners.size > 0 && !socket) connect();
  }, wait);
}

/** Subscribe to pushed alerts. Returns an unsubscribe fn; the socket opens on
 *  the first subscriber and closes when the last one leaves. */
export function subscribeAlertStream(cb: Listener): () => void {
  listeners.add(cb);
  if (!started) {
    started = true;
    connect();
  }
  return () => {
    listeners.delete(cb);
    if (listeners.size === 0) {
      started = false;
      closedByUs = true;
      try {
        socket?.close();
      } catch {
        // ignore
      }
      socket = null;
    }
  };
}

// Tear down / re-arm on auth changes (emitted by auth.ts setTokens/clearTokens).
// Without the logout teardown the socket — authorized only at $connect — would
// keep delivering pushes to a logged-out user until its 2h TTL.
if (typeof window !== "undefined") {
  window.addEventListener("dbops:auth-logout", () => {
    suspended = true;
    closedByUs = true;
    try {
      socket?.close();
    } catch {
      // ignore
    }
    socket = null;
  });
  window.addEventListener("dbops:auth-login", () => {
    suspended = false;
    closedByUs = false;
    backoff = 1000;
    if (listeners.size > 0 && !socket) connect();
  });
}
