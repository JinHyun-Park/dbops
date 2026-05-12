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
      setErr("Passwords don't match");
      return;
    }
    if (password.length < 8) {
      setErr("Password must be at least 8 characters");
      return;
    }
    setBusy(true);
    try {
      await confirmPasswordReset(email.trim(), code.trim(), password);
      // Try to sign in immediately so the user lands on the app.
      try {
        await signIn(email.trim(), password);
        router.replace("/");
      } catch {
        router.replace("/login");
      }
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "Reset failed");
      setBusy(false);
    }
  };

  return (
    <AuthLayout
      eyebrow="recover"
      title="New password"
      subtitle="Paste the code we emailed, then pick a new password"
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="Email">
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            className="w-full bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm focus:outline-none focus:border-amber-500/60"
          />
        </Field>
        <Field label="Verification code">
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
        <Field label="New password">
          <input
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            className="w-full bg-zinc-950 border border-zinc-800 px-3 py-2 text-sm focus:outline-none focus:border-amber-500/60"
          />
        </Field>
        <Field label="Confirm">
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
          {busy ? "resetting…" : "Reset password"}
        </button>
      </form>
      <div className="flex items-center justify-between text-xs text-zinc-500 pt-4">
        <Link href="/forgot" className="hover:text-amber-300 transition-colors">
          ← Resend code
        </Link>
        <Link href="/login" className="hover:text-amber-300 transition-colors">
          Sign in
        </Link>
      </div>
    </AuthLayout>
  );
}
