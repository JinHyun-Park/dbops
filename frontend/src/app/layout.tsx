import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import { CommandPalette } from "@/components/design-system/command-palette";
import { AuthButton } from "@/components/auth-button";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "DBOps",
  description: "Database Operations Dashboard",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <CommandPalette />
        <nav className="bg-zinc-800 border-b border-zinc-700 px-6 py-3 flex items-center gap-6">
          <span className="text-zinc-100 font-semibold text-sm">DBOps</span>
          <Link href="/chat" className="text-sm text-zinc-400 hover:text-zinc-100 transition-colors">
            Chat
          </Link>
          <Link href="/dashboard" className="text-sm text-zinc-400 hover:text-zinc-100 transition-colors">
            Dashboard
          </Link>
          <Link href="/query-lab" className="text-sm text-zinc-400 hover:text-zinc-100 transition-colors">
            Query Lab
          </Link>
          <Link href="/reports" className="text-sm text-zinc-400 hover:text-zinc-100 transition-colors">
            Reports
          </Link>
          <Link href="/approvals" className="text-sm text-zinc-400 hover:text-zinc-100 transition-colors">
            Approvals
          </Link>
          <Link href="/clusters" className="text-sm text-zinc-400 hover:text-zinc-100 transition-colors">
            Clusters
          </Link>
          <div className="ml-auto">
            <AuthButton />
          </div>
        </nav>
        {children}
      </body>
    </html>
  );
}
