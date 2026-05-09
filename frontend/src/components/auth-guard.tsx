"use client";

import { useEffect, useState } from "react";
import { isLoggedIn, getLoginUrl, parseTokensFromHash, setTokens } from "@/lib/auth";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const [checked, setChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);

  useEffect(() => {
    const hashTokens = parseTokensFromHash();
    if (hashTokens) {
      setTokens(hashTokens.id_token, hashTokens.access_token);
      window.location.replace("/");
      return;
    }

    if (isLoggedIn()) {
      setAuthenticated(true);
      setChecked(true);
      return;
    }

    window.location.href = getLoginUrl();
  }, []);

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
