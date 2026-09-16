"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { isLoggedIn, getUserFromToken, clearTokens } from "@/lib/auth";
import { useT } from "@/lib/i18n";

export function AuthButton() {
  const [loggedIn, setLoggedIn] = useState(false);
  const [email, setEmail] = useState<string | null>(null);
  const t = useT();

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
        {t("로그인")}
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
          // Credentials are dropped by clearTokens() and by nothing else. The
          // navigation below is a UI concern only, so do not read it as part
          // of the security boundary.
          clearTokens();
          // A HARD navigation, for the one reason that holds: router.push()
          // keeps the SPA mounted, so React component state in every open page
          // (cluster lists, metrics, chat transcripts) stays on screen behind
          // the redirect. A full load unmounts all of it.
          //
          // `replace` rather than `href =`, which also drops the authenticated
          // URL from back-history. Not flagged by
          // @next/next/no-location-assign-relative-destination, which hooks
          // only `assign()` calls and `href` assignments, so this needs no
          // suppression, and it matches the two existing call sites in
          // auth-guard.tsx and agentcore-sse.ts.
          window.location.replace("/login");
        }}
        className="text-xs text-zinc-500 hover:text-zinc-200 transition-colors"
      >
        {t("로그아웃")}
      </button>
    </div>
  );
}
