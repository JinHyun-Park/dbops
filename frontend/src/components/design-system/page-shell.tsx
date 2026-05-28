"use client";

import Link from "next/link";

interface PageHeaderProps {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: React.ReactNode;
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: PageHeaderProps) {
  return (
    <header className="flex flex-col md:flex-row md:items-end md:justify-between gap-4 md:gap-6 mb-8 md:mb-10 pb-6 md:pb-7 border-b border-zinc-800/80">
      <div>
        {eyebrow && (
          <div className="text-[11px] font-medium text-zinc-500 mb-2">
            {eyebrow}
          </div>
        )}
        <h1 className="text-2xl md:text-[34px] leading-[1.15] font-semibold tracking-tight text-zinc-50">
          {title}
        </h1>
        {description && (
          <p className="mt-3 text-[15px] leading-relaxed text-zinc-400 max-w-2xl">
            {description}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex items-center gap-2 flex-shrink-0">{actions}</div>
      )}
    </header>
  );
}

interface PageBodyProps {
  children: React.ReactNode;
}

export function PageBody({ children }: PageBodyProps) {
  return <div className="max-w-7xl mx-auto p-4 md:p-8 lg:p-10">{children}</div>;
}

interface EmptyStateProps {
  eyebrow?: string;
  title: string;
  description?: React.ReactNode;
  primary?: { href?: string; onClick?: () => void; label: string };
  secondary?: { href?: string; onClick?: () => void; label: string };
}

export function EmptyState({
  eyebrow,
  title,
  description,
  primary,
  secondary,
}: EmptyStateProps) {
  return (
    <div className="border border-dashed border-zinc-800 bg-zinc-900/30 py-16 px-8 text-center">
      {eyebrow && (
        <div className="text-[11px] font-medium text-zinc-500 mb-3">
          {eyebrow}
        </div>
      )}
      <div className="text-zinc-200 text-lg font-medium tracking-tight mb-2">
        {title}
      </div>
      {description && (
        <div className="text-sm text-zinc-500 max-w-md mx-auto mb-6">
          {description}
        </div>
      )}
      <div className="flex items-center justify-center gap-3">
        {primary &&
          (primary.href ? (
            <Link
              href={primary.href}
              className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
            >
              {primary.label}
            </Link>
          ) : (
            <button
              onClick={primary.onClick}
              className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
            >
              {primary.label}
            </button>
          ))}
        {secondary &&
          (secondary.href ? (
            <Link
              href={secondary.href}
              className="text-xs font-medium px-4 py-2 border border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
            >
              {secondary.label}
            </Link>
          ) : (
            <button
              onClick={secondary.onClick}
              className="text-xs font-medium px-4 py-2 border border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
            >
              {secondary.label}
            </button>
          ))}
      </div>
    </div>
  );
}

type Accent = "neutral" | "amber" | "rose" | "emerald" | "sky";

const ACCENT_TEXT: Record<Accent, string> = {
  neutral: "text-zinc-100",
  amber: "text-amber-400",
  rose: "text-rose-400",
  emerald: "text-emerald-400",
  sky: "text-sky-300",
};

interface StatProps {
  label: string;
  value: React.ReactNode;
  hint?: string;
  accent?: Accent;
  loading?: boolean;
}

export function Stat({
  label,
  value,
  hint,
  accent = "neutral",
  loading,
}: StatProps) {
  return (
    <div className="bg-zinc-950 px-6 py-5 border-zinc-800">
      <div className="font-mono text-[10px] tracking-[0.2em] uppercase text-zinc-500 mb-2">
        {label}
      </div>
      <div
        className={`text-3xl font-semibold tracking-tight tabular-nums ${ACCENT_TEXT[accent]}`}
      >
        {loading ? <span className="text-zinc-700">···</span> : value}
      </div>
      {hint && <div className="text-[11px] text-zinc-500 mt-1">{hint}</div>}
    </div>
  );
}

interface StatRowProps {
  children: React.ReactNode;
  cols?: 2 | 3 | 4 | 5;
}

export function StatRow({ children, cols = 4 }: StatRowProps) {
  const grid =
    cols === 2
      ? "md:grid-cols-2"
      : cols === 3
        ? "md:grid-cols-3"
        : cols === 5
          ? "md:grid-cols-3 lg:grid-cols-5"
          : "md:grid-cols-2 lg:grid-cols-4";
  return (
    <div
      className={`grid grid-cols-1 ${grid} gap-px bg-zinc-800 border border-zinc-800`}
    >
      {children}
    </div>
  );
}

interface SectionProps {
  eyebrow?: string;
  title?: string;
  description?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}

export function Section({
  eyebrow,
  title,
  description,
  actions,
  children,
}: SectionProps) {
  return (
    <section className="mb-10">
      {(eyebrow || title || actions) && (
        <div className="flex items-end justify-between gap-4 mb-4">
          <div>
            {eyebrow && (
              <div className="text-[11px] font-medium text-zinc-500 mb-1">
                {eyebrow}
              </div>
            )}
            {title && (
              <h2 className="text-base font-medium text-zinc-200 tracking-tight">
                {title}
              </h2>
            )}
            {description && (
              <div className="text-xs text-zinc-500 mt-0.5">{description}</div>
            )}
          </div>
          {actions && (
            <div className="flex items-center gap-2 flex-shrink-0">
              {actions}
            </div>
          )}
        </div>
      )}
      {children}
    </section>
  );
}
