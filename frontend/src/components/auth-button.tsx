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
        Sign in
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
          window.location.href = "/login";
        }}
        className="text-xs text-zinc-500 hover:text-zinc-200 transition-colors"
      >
        Logout
      </button>
    </div>
  );
}
