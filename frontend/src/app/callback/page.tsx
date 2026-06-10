"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { parseTokensFromHash, setTokens } from "@/lib/auth";

export default function CallbackPage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const tokens = parseTokensFromHash();
    if (tokens) {
      setTokens(tokens.id_token, tokens.access_token);
      window.location.hash = "";
      router.replace("/");
    } else {
      setError("토큰을 받지 못했습니다. 다시 로그인해 주세요.");
    }
  }, [router]);

  if (error) {
    return (
      <div className="min-h-screen bg-zinc-900 text-zinc-100 flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-400 text-lg mb-2">Login Failed</div>
          <div className="text-zinc-400 text-sm mb-4">{error}</div>
          <a href="/" className="text-blue-400 hover:text-blue-300 text-sm">
            Try again
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 flex items-center justify-center">
      <div className="text-zinc-400">Logging in...</div>
    </div>
  );
}
