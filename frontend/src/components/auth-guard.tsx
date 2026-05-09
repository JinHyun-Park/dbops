"use client";

import { useEffect, useState } from "react";
import { isLoggedIn, getLoginUrl } from "@/lib/auth";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const [checked, setChecked] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);

  useEffect(() => {
    if (isLoggedIn()) {
      setAuthenticated(true);
    } else {
      window.location.href = getLoginUrl();
    }
    setChecked(true);
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
