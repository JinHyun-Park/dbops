"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { requestPasswordReset } from "@/lib/auth";
import { AuthLayout, Field } from "@/app/login/page";

export default function ForgotPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      await requestPasswordReset(email.trim());
      router.replace(`/reset?email=${encodeURIComponent(email.trim())}`);
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : "Failed to send reset code");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthLayout
      eyebrow="recover"
      title="Reset password"
      subtitle="We'll email a verification code"
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="Email">
          <input
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
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
          {busy ? "sending…" : "Send code"}
        </button>
      </form>
      <div className="flex items-center justify-between text-xs text-zinc-500 pt-4">
        <Link href="/login" className="hover:text-amber-300 transition-colors">
          ← Back to sign in
        </Link>
      </div>
    </AuthLayout>
  );
}
