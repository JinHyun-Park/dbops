import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { CommandPalette } from "@/components/design-system/command-palette";
import { AppShell } from "@/components/app-shell";
import "./globals.css";

// Geist is the Vercel/Linear-adjacent default. Replaces IBM Plex which
// reads as the "AI-tool default" font. We keep both --font-plex-sans /
// --font-plex-mono CSS variables so existing class references continue
// to resolve — the swap is invisible to component code.
const geistSans = Geist({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
});

const geistMono = Geist_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

export const metadata: Metadata = {
  title: "DBOps · Aurora operations console",
  description:
    "AI-powered DBA workflows for Aurora MySQL and PostgreSQL at fleet scale.",
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
      <head>
        {/* Pre-hydration theme apply: avoids a flash when a user has
            switched to light. Runs before React renders. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "(function(){try{var t=localStorage.getItem('dbops_theme')||'dark';document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();",
          }}
        />
      </head>
      <body className="min-h-full">
        <CommandPalette />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
