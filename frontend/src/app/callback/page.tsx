"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { parseTokensFromHash, setTokens } from "@/lib/auth";
import { useT } from "@/lib/i18n";

export default function CallbackPage() {
  const router = useRouter();
  const t = useT();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const tokens = parseTokensFromHash();
    if (tokens) {
      setTokens(tokens.id_token, tokens.access_token);
      window.location.hash = "";
      router.replace("/");
    } else {
      // The Korean stays here as the en.ts key and is translated at the render
      // site below. t() inside this effect would join the dep array, and t
      // changes identity the moment LocaleProvider resolves the locale (one
      // tick after mount), re-running the parse against an already-cleared
      // hash and flashing this failure screen over a successful sign-in.
      setError("토큰을 받지 못했습니다. 다시 로그인해 주세요.");
    }
  }, [router]);

  if (error) {
    return (
      <div className="min-h-screen bg-zinc-900 text-zinc-100 flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-400 text-lg mb-2">{t("로그인 실패")}</div>
          <div className="text-zinc-400 text-sm mb-4">{t(error)}</div>
          <a href="/" className="text-blue-400 hover:text-blue-300 text-sm">
            {t("다시 시도")}
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 flex items-center justify-center">
      <div className="text-zinc-400">{t("로그인 중…")}</div>
    </div>
  );
}
