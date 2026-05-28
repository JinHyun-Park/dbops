"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AuthGuard } from "@/components/auth-guard";
import { AuthButton } from "@/components/auth-button";
import { ThemeToggle } from "@/components/theme-toggle";

interface NavItem {
  href: string;
  label: string;
  hint?: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const NAV: NavGroup[] = [
  {
    label: "Monitor",
    items: [
      { href: "/fleet", label: "Fleet", hint: "all clusters at a glance" },
      {
        href: "/dashboard",
        label: "Dashboard",
        hint: "single-cluster deep dive",
      },
      {
        href: "/compare",
        label: "Compare",
        hint: "cluster vs cluster · period vs period",
      },
      {
        href: "/slo",
        label: "SLO",
        hint: "availability + latency · error budget",
      },
      {
        href: "/schema",
        label: "Schema",
        hint: "FK lineage · table dependencies",
      },
    ],
  },
  {
    label: "Automate",
    items: [
      { href: "/chat", label: "Chat", hint: "natural-language ops" },
      {
        href: "/query-lab",
        label: "Query Lab",
        hint: "SQL analysis + EXPLAIN",
      },
      { href: "/approvals", label: "Approvals", hint: "DBA gate for writes" },
      {
        href: "/ask",
        label: "Ask the fleet",
        hint: "자연어 fleet 질의 + Saved views",
      },
      {
        href: "/runbooks",
        label: "Runbooks",
        hint: "AI 진단 + 처방 재사용",
      },
      {
        href: "/simulator",
        label: "Simulator",
        hint: "upgrade · param · scaling · DDL — what-if",
      },
    ],
  },
  {
    label: "Configure",
    items: [
      { href: "/alerts", label: "Alerts", hint: "rules + SNS subscribers" },
      { href: "/clusters", label: "Clusters", hint: "register + connection" },
      { href: "/reports", label: "Reports", hint: "scheduled summaries" },
      { href: "/cost", label: "Cost", hint: "Bedrock spend by model" },
    ],
  },
];

function humanize(segment: string): string {
  const map: Record<string, string> = {
    fleet: "Fleet",
    dashboard: "Dashboard",
    compare: "Compare",
    chat: "Chat",
    "query-lab": "Query Lab",
    approvals: "Approvals",
    runbooks: "Runbooks",
    ask: "Ask the fleet",
    simulator: "Simulator",
    slo: "SLO",
    schema: "Schema",
    alerts: "Alerts",
    clusters: "Clusters",
    reports: "Reports",
    cost: "Cost",
    callback: "Login",
  };
  return map[segment] || segment;
}

function Breadcrumbs({ pathname }: { pathname: string }) {
  const segments = pathname.split("/").filter(Boolean);
  if (segments.length === 0) return null;
  const crumbs = segments.map((seg, i) => ({
    label: humanize(seg),
    href: "/" + segments.slice(0, i + 1).join("/"),
  }));
  return (
    <nav className="flex items-center gap-1.5 text-[11px] uppercase tracking-[0.12em] text-zinc-500">
      <Link href="/" className="hover:text-zinc-300 transition-colors">
        home
      </Link>
      {crumbs.map((c, i) => (
        <span key={c.href} className="flex items-center gap-1.5">
          <span className="text-zinc-700">/</span>
          {i === crumbs.length - 1 ? (
            <span className="text-zinc-300">{c.label}</span>
          ) : (
            <Link
              href={c.href}
              className="hover:text-zinc-300 transition-colors"
            >
              {c.label}
            </Link>
          )}
        </span>
      ))}
    </nav>
  );
}

function SidebarItem({ item, active }: { item: NavItem; active: boolean }) {
  // Linear-style nav row: the active state is just a full-width subtle bg +
  // bolder label. No coloured dot, no left-edge bar — those were the noisy
  // accents the audit called out. The hint stays as a quiet sub-label.
  return (
    <Link
      href={item.href}
      className={`group block px-3 py-2 -mx-2 rounded text-sm transition-colors ${
        active
          ? "bg-zinc-800/80 text-zinc-100 font-medium"
          : "text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/40"
      }`}
    >
      <div>{item.label}</div>
      {item.hint && (
        <div className="mt-0.5 text-[10px] text-zinc-600 group-hover:text-zinc-500 leading-tight">
          {item.hint}
        </div>
      )}
    </Link>
  );
}

// Bottom tab bar — visible only on mobile. Picks the 5 most-used routes so
// it stays usable with one thumb. The full sidebar is still reachable via
// /more (or by widening the window) — for now we just bias to the top use cases.
const MOBILE_TABS: { href: string; label: string; icon: string }[] = [
  { href: "/fleet", label: "Fleet", icon: "▦" },
  { href: "/dashboard", label: "Dashboard", icon: "◉" },
  { href: "/chat", label: "Chat", icon: "✦" },
  { href: "/alerts", label: "Alerts", icon: "◬" },
  { href: "/clusters", label: "Clusters", icon: "⊟" },
];

function MobileTabBar({ pathname }: { pathname: string }) {
  return (
    <nav className="md:hidden fixed bottom-0 left-0 right-0 z-40 bg-zinc-950/95 backdrop-blur border-t border-zinc-800 grid grid-cols-5">
      {MOBILE_TABS.map((t) => {
        const active = pathname === t.href || pathname.startsWith(t.href + "/");
        return (
          <Link
            key={t.href}
            href={t.href}
            className={`flex flex-col items-center justify-center py-2 transition-colors ${
              active ? "text-amber-300" : "text-zinc-500 hover:text-zinc-200"
            }`}
          >
            <span className="text-base leading-none">{t.icon}</span>
            <span className="text-[10px] mt-0.5 tracking-wide">{t.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "/";

  // Auth pages should not be wrapped in the chrome.
  if (
    pathname.startsWith("/callback") ||
    pathname.startsWith("/login") ||
    pathname.startsWith("/forgot") ||
    pathname.startsWith("/reset")
  ) {
    return (
      <AuthGuard>
        <div className="min-h-screen bg-zinc-950">{children}</div>
      </AuthGuard>
    );
  }

  return (
    <AuthGuard>
      <div className="flex h-screen bg-zinc-950 text-zinc-100">
        <MobileTabBar pathname={pathname} />
        <aside className="hidden md:flex w-60 flex-col border-r border-zinc-800 bg-zinc-950">
          <Link
            href="/"
            className="px-6 py-5 border-b border-zinc-800 hover:bg-zinc-900/50 transition-colors"
          >
            <div className="font-mono text-[10px] tracking-[0.25em] text-amber-400/80 uppercase">
              dbops
            </div>
            <div className="font-semibold text-zinc-100 mt-0.5">Operations</div>
          </Link>
          <nav className="flex-1 overflow-y-auto px-4 py-4 space-y-6">
            {NAV.map((group) => (
              <div key={group.label}>
                <div className="px-3 mb-1.5 text-[10px] uppercase tracking-[0.18em] text-zinc-600 font-medium">
                  {group.label}
                </div>
                <div className="space-y-0.5">
                  {group.items.map((item) => {
                    const active =
                      pathname === item.href ||
                      (item.href !== "/" &&
                        pathname.startsWith(item.href + "/"));
                    return (
                      <SidebarItem
                        key={item.href}
                        item={item}
                        active={active}
                      />
                    );
                  })}
                </div>
              </div>
            ))}
          </nav>
          <div className="border-t border-zinc-800 px-4 py-4">
            <AuthButton />
          </div>
        </aside>

        <div className="flex-1 flex flex-col min-w-0">
          <header className="flex-shrink-0 border-b border-zinc-800 px-6 py-3 flex items-center justify-between bg-zinc-950/80 backdrop-blur">
            <Breadcrumbs pathname={pathname} />
            <div className="flex items-center gap-3">
              <ThemeToggle />
              <div className="md:hidden">
                <AuthButton />
              </div>
            </div>
          </header>
          <main className="flex-1 overflow-y-auto pb-14 md:pb-0">
            {children}
          </main>
        </div>
      </div>
    </AuthGuard>
  );
}
