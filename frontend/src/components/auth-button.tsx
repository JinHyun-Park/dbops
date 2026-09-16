"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { isLoggedIn, getUserFromToken, clearTokens } from "@/lib/auth";

export function AuthButton() {
  const [loggedIn, setLoggedIn] = useState(false);
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    setLoggedIn(isLoggedIn());
    const user = getUserFromToken();
    if (user?.email) setEmail(user.email);
  }, []);

  if (!loggedIn) {
    return (
      <Link
        href="/login"
        className="text-xs px-3 py-1.5 bg-amber-500 text-zinc-950 font-medium hover:bg-amber-400 transition-colors"
      >
        로그인
      </Link>
    );
  }

  return (
    <div className="flex items-center gap-3">
      <span
        className="text-xs text-zinc-400 truncate max-w-[140px]"
        title={email || ""}
      >
        {email}
      </span>
      <button
        onClick={() => {
          clearTokens();
          // A HARD navigation on purpose, which is why the Next rule is
          // suppressed rather than obeyed here. router.push() keeps the SPA
          // alive, so every mounted component holds its in-memory state after
          // the tokens are gone: the query cache, the shared cluster
          // selection, and any module that closed over a token. A full load is
          // the reliable way to drop all of it on sign-out.
          // eslint-disable-next-line @next/next/no-location-assign-relative-destination
          window.location.href = "/login";
        }}
        className="text-xs text-zinc-500 hover:text-zinc-200 transition-colors"
      >
        로그아웃
      </button>
    </div>
  );
}
