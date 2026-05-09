"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { exchangeCodeForTokens, setTokens } from "@/lib/auth";

function CallbackHandler() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const code = searchParams.get("code");
    if (!code) {
      setError("No authorization code received");
      return;
    }

    exchangeCodeForTokens(code)
      .then((tokens) => {
        setTokens(tokens.id_token, tokens.access_token, tokens.refresh_token);
        router.replace("/");
      })
      .catch((err) => {
        setError(err.message);
      });
  }, [searchParams, router]);

  if (error) {
    return (
      <div className="text-center">
        <div className="text-red-400 text-lg mb-2">Login Failed</div>
        <div className="text-zinc-400 text-sm">{error}</div>
      </div>
    );
  }

  return <div className="text-zinc-400">Logging in...</div>;
}

export default function CallbackPage() {
  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 flex items-center justify-center">
      <Suspense fallback={<div className="text-zinc-400">Loading...</div>}>
        <CallbackHandler />
      </Suspense>
    </div>
  );
}
