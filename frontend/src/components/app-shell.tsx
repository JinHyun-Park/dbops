"use client";

import { usePathname } from "next/navigation";
import { AuthGuard } from "@/components/auth-guard";

const PUBLIC_PATHS = ["/callback"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isPublic = PUBLIC_PATHS.some((p) => pathname.startsWith(p));

  if (isPublic) return <>{children}</>;

  return <AuthGuard>{children}</AuthGuard>;
}
