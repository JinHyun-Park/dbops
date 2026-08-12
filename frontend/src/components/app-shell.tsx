"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { isAdmin } from "@/lib/auth";
import { ClusterDropdown } from "@/components/design-system/cluster-dropdown";
import {
  Activity,
  ArrowLeftRight,
  Bell,
  BookOpen,
  Bot,
  Boxes,
  Map,
  Brain,
  Clock,
  Database,
  DollarSign,
  FileText,
  FileUp,
  FlaskConical,
  Gauge,
  GitCompare,
  GraduationCap,
  HeartPulse,
  Layers,
  MessageSquare,
  Network,
  PlugZap,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Target,
  UserCheck,
  Users,
  Wand2,
  X,
} from "lucide-react";
import { AuthGuard } from "@/components/auth-guard";
import { AuthButton } from "@/components/auth-button";
import { ThemeToggle } from "@/components/theme-toggle";
import { RcaProvider } from "@/components/rca/rca-drawer";
import { useAlertBadge, type AlertToast } from "@/lib/use-alert-badge";

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
  adminOnly?: boolean;
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
        hint: "전체 클러스터 한눈에 트리아지",
      },
      {
        href: "/map",
        label: "Map",
        icon: Map,
        hint: "서비스별 DB 청사진",
      },
      {
        href: "/learning",
        label: "Learning",
        icon: GraduationCap,
        hint: "조치 효과 이력 — 입증된 권장 조치 우선",
      },
      {
        href: "/dashboard",
        label: "Dashboard",
        icon: Gauge,
        hint: "단일 클러스터 심층 분석",
      },
      {
        href: "/compare",
        label: "Compare",
        icon: GitCompare,
        hint: "클러스터 간 · 기간 간 비교",
      },
      {
        href: "/slo",
        label: "SLO",
        icon: Target,
        hint: "가용성·지연 SLO · 에러 버짓",
      },
      {
        href: "/schema",
        label: "Schema",
        icon: Network,
        hint: "FK 계보 · 테이블 의존성",
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
        hint: "자연어로 운영 작업",
      },
      {
        href: "/query-lab",
        label: "Query Lab",
        icon: FlaskConical,
        hint: "SQL 분석 + EXPLAIN",
      },
      {
        href: "/approvals",
        label: "Approvals",
        icon: ShieldCheck,
        hint: "쓰기 작업 DBA 승인 게이트",
      },
      {
        href: "/scaleout",
        label: "Scale-out",
        icon: Layers,
        hint: "리더 추가·자동 예열 작업 상태 · 예열 전 취소",
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
        hint: "업그레이드·파라미터·스케일링·DDL what-if",
      },
    ],
  },
  {
    label: "Incident",
    items: [
      {
        href: "/tasks",
        label: "Tasks",
        icon: Bot,
        hint: "경보 자동 RCA·예약·수동 실행 등 에이전트 작업과 결과",
      },
      {
        href: "/timeline",
        label: "Timeline",
        icon: Clock,
        hint: "알림·이벤트·쓰기 통합 인시던트 피드",
      },
      {
        href: "/activity",
        label: "Activity",
        icon: Activity,
        hint: "누가 무엇을 승인·실행했는지 — 감사·회고용",
      },
      {
        href: "/workload-diff",
        label: "Workload diff",
        icon: ArrowLeftRight,
        hint: "두 시점 사이 쿼리 워크로드 변화",
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
        hint: "알림 룰 + SNS 구독자",
      },
      {
        href: "/clusters",
        label: "Clusters",
        icon: Database,
        hint: "클러스터 등록 + 연결 상태",
      },
      {
        href: "/reports",
        label: "Reports",
        icon: FileText,
        hint: "정기 운영 요약 리포트",
      },
      {
        href: "/cost",
        label: "Cost",
        icon: DollarSign,
        hint: "모델별 Bedrock 비용",
      },
      {
        href: "/preferences",
        label: "Memory",
        icon: Brain,
        hint: "에이전트가 기억하는 내용",
      },
      {
        href: "/settings",
        label: "Settings",
        icon: SlidersHorizontal,
        hint: "기능 토글 — 티켓팅·리포트 전달 (관리자)",
        adminOnly: true,
      },
      {
        href: "/approval-policies",
        label: "Approval policies",
        icon: UserCheck,
        adminOnly: true,
        hint: "지정 승인자 라우팅 — 클러스터·액션별 승인자 (관리자)",
      },
      {
        href: "/admin/users",
        label: "Users",
        icon: UserCheck,
        adminOnly: true,
        hint: "사용자 역할 관리 — admin · viewer (관리자)",
      },
      {
        href: "/admin/teams",
        label: "Teams",
        icon: Users,
        adminOnly: true,
        hint: "팀 관리 — 멤버·클러스터 가시성 (관리자)",
      },
      {
        href: "/context-files",
        label: "Context files",
        icon: FileUp,
        adminOnly: true,
        hint: "에이전트 참조 컨텍스트 업로드 (관리자)",
      },
      {
        href: "/onboarding",
        label: "Onboarding",
        icon: PlugZap,
        adminOnly: true,
        hint: "멤버 계정 연결 위저드 (관리자)",
      },
      {
        href: "/health",
        label: "Health",
        icon: HeartPulse,
        hint: "DBOps 자체 모니터링 — Lambda·Aurora·DDB 상태",
      },
      {
        href: "/apm",
        label: "APM",
        icon: Activity,
        hint: "EC2 앱 로그·성능 모니터링 (Java/Spring Boot)",
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
    scaleout: "Scale-out",
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
    settings: "Settings",
    "context-files": "Context files",
    onboarding: "Onboarding",
    health: "Health",
    apm: "APM",
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
      <Link href="/" className="hover:text-emerald-300 transition-colors">
        Home
      </Link>
      {crumbs.map((c, i) => (
        <span key={c.href} className="flex items-center gap-1.5">
          <span className="text-zinc-700">/</span>
          {i === crumbs.length - 1 ? (
            <span className="text-zinc-100">{c.label}</span>
          ) : (
            <Link
              href={c.href}
              className="hover:text-emerald-300 transition-colors"
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
      className={`group relative flex items-center gap-2.5 pl-3 pr-2.5 py-1.5 rounded-md text-[13px] transition-all duration-200 ${
        active
          ? "bg-zinc-800/70 text-zinc-50 font-medium shadow-[inset_0_0_0_1px_rgba(36,244,182,0.16)]"
          : "text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/40"
      }`}
    >
      {/* Active marker: a short bright bar pinned to the left edge. Makes the
          current location obvious at a glance without relying on color. */}
      <span
        className={`absolute left-0 top-1/2 -translate-y-1/2 h-4 w-0.5 rounded-full bg-emerald-300 transition-opacity ${
          active ? "opacity-100" : "opacity-0"
        }`}
      />
      <Icon
        size={15}
        strokeWidth={active ? 2.2 : 1.9}
        className={`flex-shrink-0 transition-colors ${
          active
            ? "text-emerald-200"
            : "text-zinc-500 group-hover:text-emerald-300"
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
    <nav className="md:hidden fixed bottom-0 left-0 right-0 z-40 bg-zinc-950/95 backdrop-blur-xl border-t border-zinc-800 grid grid-cols-5">
      {MOBILE_TABS.map((t) => {
        const active = pathname === t.href || pathname.startsWith(t.href + "/");
        const Icon = t.icon;
        return (
          <Link
            key={t.href}
            href={t.href}
            className={`flex flex-col items-center justify-center py-2 transition-colors ${
              active ? "text-emerald-200" : "text-zinc-500 hover:text-zinc-200"
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

// ── Alert badge + toast system ──────────────────────────────────────────────

function AlertBadgeButton({
  criticalCount,
  warningCount,
  onClick,
}: {
  criticalCount: number;
  warningCount: number;
  onClick: () => void;
}) {
  const total = criticalCount + warningCount;
  const badgeColor =
    criticalCount > 0
      ? "bg-rose-500 text-white"
      : warningCount > 0
        ? "bg-amber-500 text-zinc-950"
        : "bg-zinc-700 text-zinc-300";

  const ariaLabel =
    total === 0
      ? "알림 없음"
      : `알림 ${total}개 — critical ${criticalCount}, warning ${warningCount}`;

  return (
    <button
      onClick={onClick}
      aria-label={ariaLabel}
      title={ariaLabel}
      className="relative flex items-center justify-center w-8 h-8 rounded-md text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/60 transition-colors"
    >
      <Bell size={16} strokeWidth={1.8} />
      {total > 0 && (
        <span
          aria-hidden="true"
          className={`absolute -top-0.5 -right-0.5 min-w-[16px] h-4 flex items-center justify-center rounded-full text-[10px] font-mono font-medium leading-none px-1 ${badgeColor}`}
        >
          {total > 99 ? "99+" : total}
        </span>
      )}
    </button>
  );
}

function ToastItem({
  toast,
  onDismiss,
}: {
  toast: AlertToast;
  onDismiss: (id: string) => void;
}) {
  const router = useRouter();
  const href =
    toast.href ||
    (toast.cluster_id
      ? `/dashboard?cluster=${encodeURIComponent(toast.cluster_id)}`
      : null);

  const handleClick = useCallback(() => {
    if (href) router.push(href);
  }, [href, router]);

  const colorClass =
    toast.severity === "critical"
      ? "border-rose-500/60 bg-rose-500/10 text-rose-200"
      : "border-amber-500/60 bg-amber-500/10 text-amber-200";

  const hoverClass = href
    ? "cursor-pointer hover:brightness-125 hover:border-opacity-90 transition-[filter,border-color]"
    : "";

  return (
    <div
      role="alert"
      onClick={href ? handleClick : undefined}
      className={`flex items-start gap-2.5 border px-3 py-2.5 text-xs font-mono shadow-lg backdrop-blur-sm ${colorClass} ${hoverClass}`}
    >
      <span className="flex-1 leading-snug">{toast.message}</span>
      {href && (
        <span className="flex-shrink-0 opacity-50 text-[9px] uppercase tracking-wider mt-px self-center">
          →
        </span>
      )}
      <button
        onClick={(e) => {
          e.stopPropagation();
          onDismiss(toast.id);
        }}
        aria-label="알림 닫기"
        className="flex-shrink-0 opacity-60 hover:opacity-100 transition-opacity mt-px"
      >
        <X size={12} />
      </button>
    </div>
  );
}

function ToastStack({
  toasts,
  onDismiss,
}: {
  toasts: AlertToast[];
  onDismiss: (id: string) => void;
}) {
  if (toasts.length === 0) return null;
  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="fixed bottom-16 right-4 md:bottom-4 z-50 flex flex-col gap-2 w-72 max-w-[calc(100vw-2rem)]"
    >
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

// ── AppShell ─────────────────────────────────────────────────────────────────

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "/";
  const router = useRouter();
  const { criticalCount, warningCount, toasts, dismissToast, markSeen } =
    useAlertBadge();

  const [admin, setAdmin] = useState(false);
  useEffect(() => {
    setAdmin(isAdmin());
  }, []);

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
      <RcaProvider>
        <div className="flex h-screen bg-zinc-950 text-zinc-100">
          <MobileTabBar pathname={pathname} />
          {/* `flex max-md:hidden`, NOT `hidden md:flex`. Both express the same
              intent, but the second one only works if `md:flex` out-ranks
              `hidden` in the cascade, and anything that injects CSS into the
              page (a browser extension did exactly this) can flip that and
              erase the whole sidebar. This form only ever declares
              display:none when we actually want it hidden, so nothing has to
              win a fight. Same reason for the other max-* sites in this repo:
              do not "simplify" it back. */}
          <aside className="flex max-md:hidden w-60 flex-col border-r border-zinc-800 bg-zinc-950/95 backdrop-blur-xl">
            <Link
              href="/"
              className="px-5 py-4 hover:bg-zinc-900/50 transition-colors flex items-center gap-2.5"
            >
              <span className="relative flex h-7 w-7 items-center justify-center rounded-md border border-emerald-300/30 bg-emerald-300/10 shadow-[0_0_24px_rgba(36,244,182,0.16)]">
                <span className="h-2.5 w-2.5 rounded-[2px] bg-emerald-300 rotate-45" />
              </span>
              <span className="text-lg font-semibold tracking-tight text-zinc-100">
                DBOps
              </span>
            </Link>

            {/* Search trigger — makes the (previously keyboard-only) command
              palette discoverable. Shares the open path with ⌘K. */}
            <div className="px-3 pb-3">
              <button
                onClick={openCommandPalette}
                className="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md border border-zinc-800 bg-zinc-900/40 text-zinc-500 hover:text-zinc-300 hover:border-emerald-500/40 transition-colors"
              >
                <Search size={14} strokeWidth={2} className="flex-shrink-0" />
                <span className="text-[13px]">Search</span>
                <kbd className="ml-auto text-[10px] font-sans text-zinc-600 border border-zinc-700 rounded px-1 py-px bg-zinc-950/70">
                  ⌘K
                </kbd>
              </button>
            </div>

            <nav className="flex-1 overflow-y-auto px-3 pb-4 space-y-5">
              {NAV.map((group) => {
                const visibleItems = group.items.filter(
                  (item) => !item.adminOnly || admin,
                );
                if (visibleItems.length === 0) return null;
                return (
                  <div key={group.label}>
                    <div className="px-3 mb-1 text-[10px] tracking-[0.16em] text-zinc-600 font-semibold uppercase">
                      {group.label}
                    </div>
                    <div className="space-y-0.5">
                      {visibleItems.map((item) => {
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
                );
              })}
            </nav>
            <div className="border-t border-zinc-800 px-4 py-4 bg-zinc-900/30">
              <AuthButton />
            </div>
          </aside>

          <div className="flex-1 flex flex-col min-w-0">
            {/* relative z-40 is load-bearing: backdrop-blur creates a stacking
                context, and with z-index auto the SIBLING <main>'s positioned
                content (PageHeader etc.) hit-tested ABOVE the header's dropdown
                popover — the menu looked fine but real clicks fell through to
                the page underneath. */}
            <header
              data-app-header
              className="relative z-40 flex-shrink-0 border-b border-zinc-800 px-6 py-3 flex items-center justify-between bg-zinc-950/80 backdrop-blur-xl"
            >
              <Breadcrumbs pathname={pathname} />
              <div className="flex items-center gap-2">
                {/* Alert badge — bell icon with live critical/warning count.
                    Clicking navigates to /fleet and marks the current count
                    as "seen" so a stable fleet doesn't re-toast on next load. */}
                <AlertBadgeButton
                  criticalCount={criticalCount}
                  warningCount={warningCount}
                  onClick={() => {
                    markSeen();
                    router.push("/fleet");
                  }}
                />
                {/* Cluster switcher — a real dropdown, not the ⌘K palette. Hidden
                  on /chat, which manages its own per-conversation cluster. */}
                {!pathname.startsWith("/chat") && (
                  <ClusterDropdown align="right" />
                )}
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
        {/* Toast stack — fixed to the viewport, above the mobile tab bar */}
        <ToastStack toasts={toasts} onDismiss={dismissToast} />
      </RcaProvider>
    </AuthGuard>
  );
}
