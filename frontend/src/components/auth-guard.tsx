"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { clearTokens, isLoggedIn, parseTokensFromHash, refreshSession, setTokens } from "@/lib/auth";

const PUBLIC_PATHS = ["/login", "/forgot", "/reset", "/callback"];

// Cognito access tokens default to 1h. Refresh every 45 minutes so an idle
// tab survives long beyond a single token's life without surprising the user
// with a mid-action "session expired".
const REFRESH_INTERVAL_MS = 45 * 60 * 1000;

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const [checked, setChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const pathname = usePathname() || "/";
  const router = useRouter();

  useEffect(() => {
    if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
      setChecked(true);
      setAuthenticated(false);
      return;
    }

    const hashTokens = parseTokensFromHash();
    if (hashTokens) {
      setTokens(hashTokens.id_token, hashTokens.access_token);
      window.location.replace("/");
      return;
    }

    // Try a silent refresh once on mount to cover tabs reopened after >1h idle.
    // If neither cached token nor refresh works, kick to /login.
    let cancelled = false;
    (async () => {
      if (isLoggedIn()) {
        setAuthenticated(true);
        setChecked(true);
        return;
      }
      const ok = await refreshSession();
      if (cancelled) return;
      if (ok) {
        setAuthenticated(true);
        setChecked(true);
      } else {
        router.replace(`/login?next=${encodeURIComponent(pathname)}`);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname, router]);

  // Background silent refresh while the user is in the app.
  useEffect(() => {
    if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) return;
    const tick = async () => {
      const ok = await refreshSession();
      if (!ok) {
        clearTokens();
        router.replace(`/login?next=${encodeURIComponent(pathname)}`);
      }
    };
    const id = window.setInterval(tick, REFRESH_INTERVAL_MS);
    // Also refresh on window focus, in case the laptop was asleep past 1h.
    const onFocus = () => {
      void tick();
    };
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, [pathname, router]);

  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return <>{children}</>;
  }

  if (!checked) {
    return (
      <div className="min-h-screen bg-zinc-950 flex items-center justify-center">
        <div className="text-zinc-500 text-sm">Loading…</div>
      </div>
    );
  }

  if (!authenticated) return null;
  return <>{children}</>;
}
