import { test, expect } from "@playwright/test";

// Sign-out must actually sign out.
//
// The chain this pins shut, measured before the fix: clearTokens() removed only
// dbops_id_token and dbops_access_token, while amazon-cognito-identity-js keeps
// its own cache under CognitoIdentityServiceProvider.<clientId>.<username>.*,
// including the REFRESH token. /login is a public path, so AuthGuard lets the
// post-logout load through; navigate anywhere private afterwards and
// refreshSession() resolves the same user from LastAuthUser, reads the stored
// refreshToken and mints a fresh id+access token. Signed back in, no
// credentials, for the refresh token's whole lifetime.
//
// Asserted on STORAGE and on the landing page rather than on the button's own
// redirect: the redirect always looked correct. What was wrong was what it left
// behind.

const COGNITO_PREFIX = "CognitoIdentityServiceProvider.";

async function authKeys(page: import("@playwright/test").Page) {
  return page.evaluate((prefix) => {
    const all = Object.keys(localStorage);
    return {
      dbops: all.filter((k) => k.startsWith("dbops_")),
      cognito: all.filter((k) => k.startsWith(prefix)),
    };
  }, COGNITO_PREFIX);
}

test("로그아웃은 리프레시 토큰까지 지운다", async ({ page }) => {
  await page.goto("/dashboard");
  await expect(page.locator("main h1").first()).toBeVisible();

  // Guard: the whole test is vacuous if the session never had a cached
  // refresh token to leave behind.
  const before = await authKeys(page);
  test.skip(
    before.cognito.length === 0,
    "이 저장 상태에는 Cognito 캐시가 없어 우회를 재현할 수 없습니다",
  );
  expect(before.dbops.length, "로그인 상태여야 한다").toBeGreaterThan(0);

  await page.getByRole("button", { name: "로그아웃" }).click();
  await page.waitForURL(/\/login/);

  const after = await authKeys(page);
  expect(after.dbops, "dbops 토큰이 남아 있다").toEqual([]);
  expect(after.cognito, "Cognito 캐시(리프레시 토큰 포함)가 남아 있다").toEqual(
    [],
  );
});

test("로그아웃 후 보호 경로로 가도 다시 로그인되지 않는다", async ({
  page,
}) => {
  await page.goto("/dashboard");
  await expect(page.locator("main h1").first()).toBeVisible();

  const before = await authKeys(page);
  test.skip(
    before.cognito.length === 0,
    "이 저장 상태에는 Cognito 캐시가 없어 우회를 재현할 수 없습니다",
  );

  await page.getByRole("button", { name: "로그아웃" }).click();
  await page.waitForURL(/\/login/);

  // The actual attack step: the next person at the keyboard just navigates.
  await page.goto("/dashboard");

  // Either /login, or a dashboard that is demonstrably not authenticated. The
  // URL alone is not enough, so the token check below is the real assertion.
  await expect
    .poll(async () => (await authKeys(page)).dbops.length, { timeout: 15_000 })
    .toBe(0);
  await expect(page).toHaveURL(/\/login/);
});
