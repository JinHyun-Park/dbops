import { test as setup } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

// Sign in once and cache the session (Amplify tokens live in localStorage) so
// every spec reuses it via storageState. Two paths:
//   1. DBOPS_E2E_EMAIL / DBOPS_E2E_PASSWORD set → UI login, save state.
//   2. No credentials but e2e/.auth/state.json exists (e.g. harvested from a
//      logged-in browser) → reuse it as-is. The refresh token inside keeps
//      authedFetch's silent refresh working across runs.
const STATE = path.join(process.cwd(), "e2e", ".auth", "state.json");

setup("authenticate", async ({ page }) => {
  const email = process.env.DBOPS_E2E_EMAIL;
  const password = process.env.DBOPS_E2E_PASSWORD;

  if (!email || !password) {
    if (fs.existsSync(STATE)) return; // reuse cached session
    throw new Error(
      "No session available: set DBOPS_E2E_EMAIL / DBOPS_E2E_PASSWORD " +
        "(test user credentials), or place a storage state at " +
        "frontend/e2e/.auth/state.json.",
    );
  }

  await page.goto("/login");
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(password);
  await page.locator('button[type="submit"]').click();
  // Successful sign-in replaces the route with `next` (default "/").
  await page.waitForURL((u) => !u.pathname.startsWith("/login"), {
    timeout: 20_000,
  });
  fs.mkdirSync(path.dirname(STATE), { recursive: true });
  await page.context().storageState({ path: STATE });
});
