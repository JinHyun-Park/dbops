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
    eyebrow: "환영합니다",
    title: "AI 기반 Aurora 운영 콘솔",
    body: (
      <>
        DBOps는 Amazon Aurora MySQL / PostgreSQL을 플릿 단위로 운영하는 DBA를
        위해 만든 콘솔입니다. 모든 패널이 에이전트와 연결돼 있어, 이상
        징후·이벤트·발견 항목을 클릭하면{" "}
        <span className="text-sky-300">한 번에 원인 분석과 조치 제안</span>을
        받을 수 있습니다.
      </>
    ),
  },
  {
    eyebrow: "1단계",
    title: "클러스터 등록",
    body: (
      <>
        <span className="font-mono text-amber-300">Clusters</span> 페이지에서
        시작하세요. 한두 개라면 수동 등록 폼을, 플릿 규모라면{" "}
        <span className="font-mono text-sky-300">🔎 Discover clusters</span>{" "}
        버튼을 사용하면 됩니다 — DBOps가 계정 내(또는 크로스 어카운트 롤 경유)
        모든 Aurora 클러스터를 나열하고, 체크한 것만 등록합니다.
      </>
    ),
    cta: { href: "/clusters", label: "Clusters 페이지로 이동 →" },
  },
  {
    eyebrow: "2단계",
    title: "약 5분 대기",
    body: (
      <>
        ETL이 5분 주기로 메트릭, 테이블 통계, 락, 점검 결과를 수집합니다. 첫
        사이클 전까지 대시보드는{" "}
        <span className="text-zinc-400 italic">no data yet</span>으로
        표시됩니다. 이 시간 동안{" "}
        <span className="font-mono text-amber-300">Alerts</span>에서 Slack /
        PagerDuty 구독자를 등록해두면, 임계치를 초과하는 즉시 알림을 받을 수
        있습니다.
      </>
    ),
    cta: { href: "/alerts", label: "Alerts 설정하기 →" },
  },
  {
    eyebrow: "3단계",
    title: "자연어로 운영하기",
    body: (
      <>
        <span className="font-mono text-amber-300">Chat</span>을 열고 다음과
        같이 물어보세요:{" "}
        <span className="text-sky-300 italic">
          &quot;prod-pg에서 최근 슬로우 쿼리 분석해줘&quot;
        </span>{" "}
        또는{" "}
        <span className="text-sky-300 italic">
          &quot;analytics 클러스터에서 CPU가 왜 튀는지 알려줘&quot;
        </span>{" "}
        에이전트가 MCP 툴로 Performance Insights를 보고, EXPLAIN을 돌리고,
        조치를 제안합니다. 기본은 읽기 전용이며, 변경 작업은 Approval Center를
        통해서만 적용됩니다.
      </>
    ),
    cta: { href: "/chat", label: "Chat 열기 →" },
  },
];

export function OnboardingModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (open) setStep(0);
  }, [open]);

  if (!open) return null;

  const s = STEPS[step];
  const isLast = step === STEPS.length - 1;

  return (
    <div
      className="fixed inset-0 z-50 bg-zinc-950/80 backdrop-blur flex items-center justify-center p-4"
      onClick={onClose}
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
          <h2
            id="onboarding-title"
            className="text-xl font-semibold text-zinc-100 tracking-tight"
          >
            {s.title}
          </h2>
          <p className="text-sm text-zinc-300 leading-relaxed mt-3">{s.body}</p>
          {s.cta && (
            <Link
              href={s.cta.href}
              onClick={onClose}
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
                aria-label={`${i + 1}단계로 이동`}
                className={`w-1.5 h-1.5 rounded-full transition-colors ${
                  i === step ? "bg-amber-400" : "bg-zinc-700 hover:bg-zinc-500"
                }`}
              />
            ))}
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={onClose}
              className="text-[11px] text-zinc-500 hover:text-zinc-200 transition-colors"
            >
              나중에 보기
            </button>
            {!isLast ? (
              <button
                onClick={() => setStep(step + 1)}
                className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
              >
                다음
              </button>
            ) : (
              <button
                onClick={onClose}
                className="text-xs font-medium px-3 py-1.5 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
              >
                시작하기
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export function useOnboarding() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      if (!localStorage.getItem(STORAGE_KEY)) setOpen(true);
    } catch {
      /* localStorage blocked — skip auto-open */
    }
  }, []);

  const close = () => {
    try {
      localStorage.setItem(STORAGE_KEY, new Date().toISOString());
    } catch {
      /* ignore */
    }
    setOpen(false);
  };

  const reopen = () => setOpen(true);

  return { open, close, reopen };
}
