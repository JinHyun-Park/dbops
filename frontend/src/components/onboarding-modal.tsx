"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

const STORAGE_KEY = "dbops_onboarded_v1";

interface Step {
  eyebrow: string;
  title: string;
  body: React.ReactNode;
  cta?: { href: string; label: string };
}

const STEPS: Step[] = [
  {
    eyebrow: "Welcome",
    title: "AI-assisted Aurora ops",
    body: (
      <>
        DBOps is an opinionated console for DBAs running Amazon Aurora MySQL / PostgreSQL at fleet
        scale. Every panel is wired to an agent — click any finding, anomaly, or event for a
        one-click <span className="text-sky-300">Explain + fix</span>.
      </>
    ),
  },
  {
    eyebrow: "Step 1",
    title: "Register a cluster",
    body: (
      <>
        Start on the <span className="font-mono text-amber-300">Clusters</span> page. For one or
        two clusters use the manual form; for a fleet click{" "}
        <span className="font-mono text-sky-300">🔎 Discover clusters</span> — DBOps enumerates
        every Aurora cluster in your account (or cross-account via role) and registers the ones
        you check.
      </>
    ),
    cta: { href: "/clusters", label: "Go to Clusters →" },
  },
  {
    eyebrow: "Step 2",
    title: "Wait ~5 minutes",
    body: (
      <>
        ETL collects metrics, table stats, locks, and maintenance health on a 5-minute schedule.
        Until the first cycle, dashboards say{" "}
        <span className="text-zinc-400 italic">no data yet</span>. Use this window to add Slack /
        PagerDuty subscribers under <span className="font-mono text-amber-300">Alerts</span> so
        you're notified when thresholds trip.
      </>
    ),
    cta: { href: "/alerts", label: "Configure Alerts →" },
  },
  {
    eyebrow: "Step 3",
    title: "Try natural-language ops",
    body: (
      <>
        Open <span className="font-mono text-amber-300">Chat</span> and ask:{" "}
        <span className="text-sky-300 italic">
          &quot;analyze recent slow queries on prod-pg&quot;
        </span>{" "}
        or{" "}
        <span className="text-sky-300 italic">
          &quot;why is CPU spiking on my analytics cluster?&quot;
        </span>{" "}
        The agent uses MCP tools to inspect Performance Insights, run EXPLAIN, and propose
        actions — read-only by default, mutations gate through the Approval Center.
      </>
    ),
    cta: { href: "/chat", label: "Open Chat →" },
  },
];

export function OnboardingModal() {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      if (!localStorage.getItem(STORAGE_KEY)) setOpen(true);
    } catch {
      // localStorage blocked — just don't show. No big deal.
    }
  }, []);

  const close = (mark: boolean) => {
    if (mark) {
      try {
        localStorage.setItem(STORAGE_KEY, new Date().toISOString());
      } catch {
        /* ignore */
      }
    }
    setOpen(false);
  };

  if (!open) return null;

  const s = STEPS[step];
  const isLast = step === STEPS.length - 1;

  return (
    <div
      className="fixed inset-0 z-50 bg-zinc-950/80 backdrop-blur flex items-center justify-center p-4"
      onClick={() => close(true)}
      role="dialog"
      aria-modal="true"
      aria-labelledby="onboarding-title"
    >
      <div
        className="w-full max-w-lg bg-zinc-900 border border-zinc-700 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 pt-6 pb-2">
          <div className="font-mono text-[10px] tracking-[0.2em] uppercase text-amber-400/80 mb-2">
            {s.eyebrow}
          </div>
          <h2 id="onboarding-title" className="text-xl font-semibold text-zinc-100 tracking-tight">
            {s.title}
          </h2>
          <p className="text-sm text-zinc-300 leading-relaxed mt-3">{s.body}</p>
          {s.cta && (
            <Link
              href={s.cta.href}
              onClick={() => close(true)}
              className="inline-block mt-4 text-xs px-3 py-1.5 border border-amber-500/40 text-amber-300 hover:bg-amber-500/10 transition-colors"
            >
              {s.cta.label}
            </Link>
          )}
        </div>

        <div className="px-6 py-4 border-t border-zinc-800 flex items-center justify-between">
          <div className="flex items-center gap-1.5">
            {STEPS.map((_, i) => (
              <button
                key={i}
                onClick={() => setStep(i)}
                aria-label={`Go to step ${i + 1}`}
                className={`w-1.5 h-1.5 rounded-full transition-colors ${
                  i === step ? "bg-amber-400" : "bg-zinc-700 hover:bg-zinc-500"
                }`}
              />
            ))}
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => close(true)}
              className="text-[11px] text-zinc-500 hover:text-zinc-200 transition-colors"
            >
              Already familiar
            </button>
            {!isLast ? (
              <button
                onClick={() => setStep(step + 1)}
                className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
              >
                Next
              </button>
            ) : (
              <button
                onClick={() => close(true)}
                className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
              >
                Got it
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
