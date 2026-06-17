"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { signIn } from "@/lib/auth";

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-zinc-950" />}>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const sp = useSearchParams();
  const next = sp.get("next") || "/";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // When an admin-created account signs in with its temporary password, Cognito
  // demands a new one before issuing tokens. We hold that continuation here and
  // swap the form to a "set new password" step instead of dead-ending.
  const [challenge, setChallenge] = useState<
    ((newPassword: string) => Promise<unknown>) | null
  >(null);
  const [newPw, setNewPw] = useState("");
  const [newPwConfirm, setNewPwConfirm] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const r = await signIn(email.trim(), password);
      if (r.status === "new_password_required") {
        // Wrap in a fn so React stores it rather than invoking it as an updater.
        setChallenge(() => r.complete);
        setBusy(false);
        return;
      }
      router.replace(next);
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "로그인 실패");
      setBusy(false);
    }
  };

  const submitNewPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (newPw !== newPwConfirm) {
      setErr("비밀번호가 일치하지 않습니다");
      return;
    }
    if (newPw.length < 8) {
      setErr("비밀번호는 최소 8자 이상이어야 합니다");
      return;
    }
    setBusy(true);
    try {
      await challenge!(newPw);
      router.replace(next);
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "비밀번호 설정 실패");
      setBusy(false);
    }
  };

  if (challenge) {
    return (
      <AuthLayout
        eyebrow="dbops"
        title="새 비밀번호 설정"
        subtitle="최초 로그인 — 임시 비밀번호를 변경하세요"
      >
        <form onSubmit={submitNewPassword} className="space-y-4">
          <Field label="새 비밀번호">
            <input
              type="password"
              autoComplete="new-password"
              value={newPw}
              onChange={(e) => setNewPw(e.target.value)}
              required
              className="w-full bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm focus:outline-none focus:border-amber-500/60"
            />
          </Field>
          <Field label="비밀번호 확인">
            <input
              type="password"
              autoComplete="new-password"
              value={newPwConfirm}
              onChange={(e) => setNewPwConfirm(e.target.value)}
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
            {busy ? "설정 중…" : "비밀번호 설정 후 로그인"}
          </button>
        </form>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      eyebrow="dbops"
      title="로그인"
      subtitle="Aurora operations console"
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="이메일">
          <input
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            className="w-full bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm focus:outline-none focus:border-amber-500/60"
          />
        </Field>
        <Field label="비밀번호">
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
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
          {busy ? "로그인 중…" : "로그인"}
        </button>
      </form>
      <div className="flex items-center justify-between text-xs text-zinc-500 pt-4">
        <Link href="/forgot" className="hover:text-amber-300 transition-colors">
          비밀번호 찾기
        </Link>
        <span className="text-zinc-700">공개 가입 없음 · admin 전용</span>
      </div>
    </AuthLayout>
  );
}

export function AuthLayout({
  eyebrow,
  title,
  subtitle,
  children,
}: {
  eyebrow: string;
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen bg-zinc-950 flex items-center justify-center p-6">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="font-mono text-[10px] tracking-[0.25em] text-amber-400/70 uppercase">
            {eyebrow}
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-50 mt-1">
            {title}
          </h1>
          <div className="text-sm text-zinc-500 mt-1">{subtitle}</div>
        </div>
        <div className="border border-zinc-800 bg-zinc-900/50 p-6">
          {children}
        </div>
      </div>
    </div>
  );
}

export function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="block text-[10px] uppercase tracking-wider text-zinc-500 mb-1.5">
        {label}
      </label>
      {children}
    </div>
  );
}
