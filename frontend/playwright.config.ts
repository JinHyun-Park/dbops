import { defineConfig, devices } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

// Load .env.e2e (gitignored) so local runs pick up the dedicated Cognito test
// user without exporting anything. CI provides the same vars via secrets.
const envFile = path.join(__dirname, ".env.e2e");
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, "utf8").split("\n")) {
    const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
}

// Smoke E2E against a DEPLOYED environment (static export + real API), using
// REAL mouse input. Raison d'être: the header cluster dropdown once rendered
// fine but swallowed every real click (stacking-context burial) — a bug class
// that synthetic element.click() can never catch. Playwright clicks are
// hit-tested ("element intercepts pointer events" fails loudly), so these
// specs are the regression net for exactly that.
//
// Target URL: DBOPS_E2E_URL (defaults to the dev CloudFront distribution).
// Auth: see e2e/auth.setup.ts — env credentials or a cached storage state.
const BASE_URL =
  process.env.DBOPS_E2E_URL || "https://dm1xo7omariq4.cloudfront.net";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  retries: process.env.CI ? 1 : 0,
  // Specs mutate shared state (the globally selected cluster), so keep a
  // single worker — parallel workers would race each other's selection.
  workers: 1,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        storageState: "e2e/.auth/state.json",
      },
      dependencies: ["setup"],
    },
  ],
});
