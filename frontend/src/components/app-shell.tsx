"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Activity,
  ArrowLeftRight,
  Bell,
  BookOpen,
  Boxes,
  Brain,
  Clock,
  Database,
  DollarSign,
  FileText,
  FlaskConical,
  Gauge,
  GitCompare,
  HeartPulse,
  MessageSquare,
  Network,
  Search,
  ShieldCheck,
  Sparkles,
  Target,
  Wand2,
} from "lucide-react";
import { AuthGuard } from "@/components/auth-guard";
import { AuthButton } from "@/components/auth-button";
import { ThemeToggle } from "@/components/theme-toggle";

type IconType = React.ComponentType<{
  size?: number | string;
  className?: string;
  strokeWidth?: number;
}>;

interface NavItem {
  href: string;
  label: string;
  icon: IconType;
  hint?: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

// Icons give each row a distinct visual anchor — the single biggest
// scannability win once the rail grows past ~10 items. Kept monochrome
// (dim when idle, bright when active) so the palette stays neutral and
// doesn't read as a stock template.
const NAV: NavGroup[] = [
  {
    label: "Monitor",
    items: [
      {
        href: "/fleet",
        label: "Fleet",
        icon: Boxes,
        hint: "all clusters at a glance",
      },
      {
        href: "/dashboard",
        label: "Dashboard",
        icon: Gauge,
        hint: "single-cluster deep dive",
      },
      {
        href: "/compare",
        label: "Compare",
        icon: GitCompare,
        hint: "cluster vs cluster · period vs period",
      },
      {
        href: "/slo",
        label: "SLO",
        icon: Target,
        hint: "availability + latency · error budget",
      },
      {
        href: "/schema",
        label: "Schema",
        icon: Network,
        hint: "FK lineage · table dependencies",
      },
    ],
  },
  {
    label: "Automate",
    items: [
      {
        href: "/chat",
        label: "Chat",
        icon: MessageSquare,
        hint: "natural-language ops",
      },
      {
        href: "/query-lab",
        label: "Query Lab",
        icon: FlaskConical,
        hint: "SQL analysis + EXPLAIN",
      },
      {
        href: "/approvals",
        label: "Approvals",
        icon: ShieldCheck,
        hint: "DBA gate for writes",
      },
      {
        href: "/ask",
        label: "Ask the fleet",
        icon: Sparkles,
        hint: "자연어 fleet 질의 + Saved views",
      },
      {
        href: "/runbooks",
        label: "Runbooks",
        icon: BookOpen,
        hint: "AI 진단 + 처방 재사용",
      },
      {
        href: "/simulator",
        label: "Simulator",
        icon: Wand2,
        hint: "upgrade · param · scaling · DDL — what-if",
      },
    ],
  },
  {
    label: "Incident",
    items: [
      {
        href: "/timeline",
        label: "Timeline",
        icon: Clock,
        hint: "unified incident feed: alerts + events + writes",
      },
      {
        href: "/activity",
        label: "Activity",
        icon: Activity,
        hint: "who approved/executed what, for compliance + retro",
      },
      {
        href: "/workload-diff",
        label: "Workload diff",
        icon: ArrowLeftRight,
        hint: "what queries changed between two points in time",
      },
    ],
  },
  {
    label: "Configure",
    items: [
      {
        href: "/alerts",
        label: "Alerts",
        icon: Bell,
        hint: "rules + SNS subscribers",
      },
      {
        href: "/clusters",
        label: "Clusters",
        icon: Database,
        hint: "register + connection",
      },
      {
        href: "/reports",
        label: "Reports",
        icon: FileText,
        hint: "scheduled summaries",
      },
      {
        href: "/cost",
        label: "Cost",
        icon: DollarSign,
        hint: "Bedrock spend by model",
      },
      {
        href: "/preferences",
        label: "Memory",
        icon: Brain,
        hint: "what the agent remembers about you",
      },
      {
        href: "/health",
        label: "Health",
        icon: HeartPulse,
        hint: "DBOps self-monitoring — Lambda + Aurora + DDB state",
      },
    ],
  },
];

function humanize(segment: string): string {
  const map: Record<string, string> = {
    fleet: "Fleet",
    dashboard: "Dashboard",
    compare: "Compare",
    slo: "SLO",
    schema: "Schema",
    chat: "Chat",
    "query-lab": "Query Lab",
    approvals: "Approvals",
    ask: "Ask the fleet",
    runbooks: "Runbooks",
    simulator: "Simulator",
    timeline: "Timeline",
    activity: "Activity",
    "workload-diff": "Workload diff",
    alerts: "Alerts",
    clusters: "Clusters",
    reports: "Reports",
    cost: "Cost",
    preferences: "Memory",
    health: "Health",
    callback: "Login",
  };
  return map[segment] || segment;
}

function openCommandPalette() {
  // Decoupled from CommandPalette's internals — it also listens for this
  // event, so the visible button and ⌘K share one open path.
  window.dispatchEvent(new CustomEvent("dbops:open-command-palette"));
}

function Breadcrumbs({ pathname }: { pathname: string }) {
  const segments = pathname.split("/").filter(Boolean);
  if (segments.length === 0) return null;
  const crumbs = segments.map((seg, i) => ({
    label: humanize(seg),
    href: "/" + segments.slice(0, i + 1).join("/"),
  }));
  return (
    <nav className="flex items-center gap-1.5 text-[12px] text-zinc-500">
      <Link href="/" className="hover:text-zinc-300 transition-colors">
        Home
      </Link>
      {crumbs.map((c, i) => (
        <span key={c.href} className="flex items-center gap-1.5">
          <span className="text-zinc-700">/</span>
          {i === crumbs.length - 1 ? (
            <span className="text-zinc-200">{c.label}</span>
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
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      title={item.hint}
      aria-current={active ? "page" : undefined}
      className={`group relative flex items-center gap-2.5 pl-3 pr-2.5 py-1.5 rounded-md text-[13px] transition-colors ${
        active
          ? "bg-zinc-800/70 text-zinc-50 font-medium"
          : "text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/40"
      }`}
    >
      {/* Active marker: a short bright bar pinned to the left edge. Makes the
          current location obvious at a glance without relying on color. */}
      <span
        className={`absolute left-0 top-1/2 -translate-y-1/2 h-4 w-0.5 rounded-full bg-zinc-100 transition-opacity ${
          active ? "opacity-100" : "opacity-0"
        }`}
      />
      <Icon
        size={15}
        strokeWidth={active ? 2.2 : 1.9}
        className={`flex-shrink-0 transition-colors ${
          active ? "text-zinc-100" : "text-zinc-500 group-hover:text-zinc-300"
        }`}
      />
      <span className="truncate">{item.label}</span>
    </Link>
  );
}

// Bottom tab bar — mobile only. The 5 most-used routes for one-thumb reach;
// the full grouped rail is back once the viewport widens.
const MOBILE_TABS: { href: string; label: string; icon: IconType }[] = [
  { href: "/fleet", label: "Fleet", icon: Boxes },
  { href: "/dashboard", label: "Dashboard", icon: Gauge },
  { href: "/chat", label: "Chat", icon: MessageSquare },
  { href: "/alerts", label: "Alerts", icon: Bell },
  { href: "/clusters", label: "Clusters", icon: Database },
];

function MobileTabBar({ pathname }: { pathname: string }) {
  return (
    <nav className="md:hidden fixed bottom-0 left-0 right-0 z-40 bg-zinc-950/95 backdrop-blur border-t border-zinc-800 grid grid-cols-5">
      {MOBILE_TABS.map((t) => {
        const active = pathname === t.href || pathname.startsWith(t.href + "/");
        const Icon = t.icon;
        return (
          <Link
            key={t.href}
            href={t.href}
            className={`flex flex-col items-center justify-center py-2 transition-colors ${
              active ? "text-zinc-100" : "text-zinc-500 hover:text-zinc-200"
            }`}
          >
            <Icon size={18} strokeWidth={active ? 2.2 : 1.9} />
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
        <aside className="hidden md:flex w-56 flex-col border-r border-zinc-800 bg-zinc-950">
          <Link
            href="/"
            className="px-5 py-4 hover:bg-zinc-900/50 transition-colors flex items-center gap-2"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-zinc-200" />
            <span className="text-lg font-semibold tracking-tight text-zinc-100">
              DBOps
            </span>
          </Link>

          {/* Search trigger — makes the (previously keyboard-only) command
              palette discoverable. Shares the open path with ⌘K. */}
          <div className="px-3 pb-3">
            <button
              onClick={openCommandPalette}
              className="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md border border-zinc-800 bg-zinc-900/40 text-zinc-500 hover:text-zinc-300 hover:border-zinc-700 transition-colors"
            >
              <Search size={14} strokeWidth={2} className="flex-shrink-0" />
              <span className="text-[13px]">Search</span>
              <kbd className="ml-auto text-[10px] font-sans text-zinc-600 border border-zinc-700 rounded px-1 py-px">
                ⌘K
              </kbd>
            </button>
          </div>

          <nav className="flex-1 overflow-y-auto px-3 pb-4 space-y-5">
            {NAV.map((group) => (
              <div key={group.label}>
                <div className="px-3 mb-1 text-[10px] tracking-[0.14em] text-zinc-600 font-medium uppercase">
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
