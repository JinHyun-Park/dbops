"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { confirmPasswordReset, signIn } from "@/lib/auth";
import { AuthLayout, Field } from "@/app/login/page";

export default function ResetPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-zinc-950" />}>
      <ResetForm />
    </Suspense>
  );
}

function ResetForm() {
  const router = useRouter();
  const sp = useSearchParams();
  const [email, setEmail] = useState(sp.get("email") || "");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (password !== confirm) {
      setErr("비밀번호가 일치하지 않습니다");
      return;
    }
    if (password.length < 8) {
      setErr("비밀번호는 최소 8자 이상이어야 합니다");
      return;
    }
    setBusy(true);
    try {
      await confirmPasswordReset(email.trim(), code.trim(), password);
      // Try to sign in immediately so the user lands on the app. A
      // just-reset account is CONFIRMED, so signIn returns status "ok"; any
      // other outcome falls back to the login page.
      try {
        const r = await signIn(email.trim(), password);
        router.replace(r.status === "ok" ? "/" : "/login");
      } catch {
        router.replace("/login");
      }
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "재설정 실패");
      setBusy(false);
    }
  };

  return (
    <AuthLayout
      eyebrow="복구"
      title="새 비밀번호 설정"
      subtitle="이메일로 받은 인증 코드를 입력하고 새 비밀번호를 설정하세요"
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="이메일">
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            className="w-full bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm focus:outline-none focus:border-amber-500/60"
          />
        </Field>
        <Field label="인증 코드">
          <input
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            required
            className="w-full bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm font-mono focus:outline-none focus:border-amber-500/60"
          />
        </Field>
        <Field label="새 비밀번호">
          <input
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            className="w-full bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm focus:outline-none focus:border-amber-500/60"
          />
        </Field>
        <Field label="비밀번호 확인">
          <input
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
            className="w-full bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm focus:outline-none focus:border-amber-500/60"
          />
        </Field>
        {err && (
          <div className="text-xs text-rose-400 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
            {err}
          </div>
        )}
        <button
          type="submit"
          disabled={busy}
          className="w-full bg-amber-500 hover:bg-amber-400 text-zinc-950 font-medium py-2 disabled:opacity-50"
        >
          {busy ? "재설정 중…" : "비밀번호 재설정"}
        </button>
      </form>
      <div className="flex items-center justify-between text-xs text-zinc-500 pt-4">
        <Link href="/forgot" className="hover:text-amber-300 transition-colors">
          ← 코드 재발송
        </Link>
        <Link href="/login" className="hover:text-amber-300 transition-colors">
          로그인
        </Link>
      </div>
    </AuthLayout>
  );
}
