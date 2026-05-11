"use client";

import { useEffect, useState } from "react";
import { isLoggedIn, getUserFromToken, getLoginUrl, getLogoutUrl, clearTokens } from "@/lib/auth";

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
      <button
        onClick={async () => {
          window.location.href = await getLoginUrl();
        }}
        className="text-sm bg-blue-600 text-white px-3 py-1.5 rounded hover:bg-blue-500 transition-colors"
      >
        Login
      </button>
    );
  }

  return (
    <div className="flex items-center gap-3">
      <span className="text-xs text-zinc-400">{email}</span>
      <button
        onClick={async () => {
          clearTokens();
          window.location.href = await getLogoutUrl();
        }}
        className="text-sm text-zinc-400 hover:text-zinc-100 transition-colors"
      >
        Logout
      </button>
    </div>
  );
}
