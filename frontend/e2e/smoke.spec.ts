import { test, expect, type Page } from "@playwright/test";

// REAL-input smoke suite. Every interaction here goes through Playwright's
// hit-tested clicks — if an overlay/stacking bug ever buries a control again
// (the "menu renders but clicks fall through" class), these fail with
// "element intercepts pointer events" instead of silently passing the way
// synthetic element.click() checks did.

const CLUSTER_ID = /dbops-dev-sample|sample-cluster/;

// The portal popover both dropdown components render at body level.
function clusterPopover(page: Page) {
  return page
    .locator("body > div")
    .filter({ has: page.locator('input[placeholder*="검색"]') });
}

test("모든 핵심 페이지가 크래시 없이 렌더된다", async ({ page }) => {
  const pages = [
    "/dashboard",
    "/fleet",
    "/timeline",
    "/activity",
    "/runbooks",
    "/alerts",
    "/approvals",
    "/simulator",
    "/slo",
    "/schema",
    "/compare",
    "/query-lab",
    "/reports",
    "/cost",
    "/clusters",
    "/health",
    "/preferences",
    "/ask",
  ];
  for (const path of pages) {
    await page.goto(path);
    // Every page renders a PageHeader <h1>; a client crash blanks <main>.
    await expect(
      page.locator("main h1").first(),
      `${path} should render`,
    ).toBeVisible({ timeout: 15_000 });
    await expect(
      page.getByText(/Application error|Minified React error/),
      `${path} should not crash`,
    ).toHaveCount(0);
  }
});

test("헤더 클러스터 드롭다운: 실클릭으로 열고 전환하면 대시보드가 따라온다", async ({
  page,
}) => {
  await page.goto("/dashboard");
  const trigger = page
    .locator("header[data-app-header] button")
    .filter({ hasText: /dbops|sample|클러스터 선택/ })
    .first();
  await expect(trigger).toBeVisible();

  await trigger.click();
  const pop = clusterPopover(page);
  await expect(pop).toBeVisible();

  // Pick a cluster that is NOT the current one (current row carries "현재").
  const option = pop
    .locator("button")
    .filter({ hasText: CLUSTER_ID })
    .filter({ hasNotText: "현재" })
    .first();
  // The cluster id lives in the option's name span (font-mono). The whole
  // button textContent also includes the engine badge ("PG"/"MySQL") and a
  // "현재" marker, so reading the button text would yield e.g. "sample-clusterPG"
  // and the ?cluster= assertion would never match the real id "sample-cluster".
  const targetId =
    (await option.locator("span.font-mono").first().textContent())?.trim() ??
    "";
  await option.click();

  // Selection propagates. The URL ?cluster= is written by the DASHBOARD's own
  // mirror effect, so asserting it proves the page component received the new
  // selection — not just that the dropdown updated itself.
  await expect(page).toHaveURL(
    new RegExp(`cluster=${encodeURIComponent(targetId).slice(0, 24)}`),
  );
  await expect(trigger).toContainText(targetId.slice(0, 20));
  await expect(page.locator("main h1").first()).toBeVisible();
});

test("인페이지 클러스터 picker(runbooks): 실클릭으로 '모든 클러스터' 선택", async ({
  page,
}) => {
  await page.goto("/runbooks");
  const trigger = page
    .locator("main button")
    .filter({ hasText: /dbops|sample|모든 클러스터|클러스터 선택/ })
    .first();
  await trigger.click();

  const pop = clusterPopover(page);
  await expect(pop).toBeVisible();
  await pop.getByRole("button", { name: "모든 클러스터" }).click();
  await expect(trigger).toContainText("모든 클러스터");
});

test("⌘K 팔레트: 페이지 검색 전용으로 열리고 이동한다", async ({ page }) => {
  await page.goto("/dashboard");
  await page.keyboard.press("ControlOrMeta+k");
  const input = page.getByPlaceholder("페이지 검색...");
  await expect(input).toBeVisible();
  await input.fill("fleet");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/fleet/);
});

test("RCA 드로어: 페이지를 떠나지 않고 인플레이스로 열린다", async ({
  page,
}) => {
  await page.goto("/timeline");
  const rca = page.getByRole("button", { name: /AI 근본원인 분석/ });
  await expect(rca).toBeVisible();
  await rca.click();

  // Drawer header (distinct from the button text, which has no inner spaces).
  await expect(page.getByText("근본 원인 분석").first()).toBeVisible();
  await expect(page).toHaveURL(/\/timeline/); // stayed in place
  await expect(
    page.getByRole("button", { name: /전체 대화로 이어가기/ }),
  ).toBeVisible();

  // Close (aborts the throwaway agent stream) — page intact afterwards.
  await page.keyboard.press("Escape");
  await expect(page.getByText("근본 원인 분석")).toHaveCount(0);
  await expect(page.locator("main h1").first()).toBeVisible();
});
