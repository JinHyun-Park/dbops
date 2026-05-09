"use client";

import { AuthGuard } from "@/components/auth-guard";

export function AppShell({ children }: { children: React.ReactNode }) {
  return <AuthGuard>{children}</AuthGuard>;
}
