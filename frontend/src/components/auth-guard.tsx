"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { isLoggedIn, getLoginUrl, parseTokensFromHash, setTokens } from "@/lib/auth";

const PUBLIC_PATHS = ["/callback"];

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [checked, setChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);

  useEffect(() => {
    if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
      setAuthenticated(true);
      setChecked(true);
      return;
    }

    const hashTokens = parseTokensFromHash();
    if (hashTokens) {
      setTokens(hashTokens.id_token, hashTokens.access_token);
      window.location.hash = "";
      setAuthenticated(true);
      setChecked(true);
      return;
    }

    if (isLoggedIn()) {
      setAuthenticated(true);
    } else {
      window.location.href = getLoginUrl();
      return;
    }
    setChecked(true);
  }, [pathname]);

  if (!checked) {
    return (
      <div className="min-h-screen bg-zinc-900 flex items-center justify-center">
        <div className="text-zinc-400">Loading...</div>
      </div>
    );
  }

  if (!authenticated) return null;

  return <>{children}</>;
}
