import { test, expect } from "@playwright/test";

// Korean UI on purpose. i18n resolves the locale from navigator.language and
// falls back to ENGLISH, and Playwright's chromium is en-US, so an unpinned
// spec silently asserts against whichever language the runner happened to
// have. Measured: without this, `main h1` reads "RCA inbox" and every Korean
// assertion below fails for a reason that has nothing to do with the inbox.
// See the locale spec at the bottom, which pins both directions on purpose.
test.use({ locale: "ko-KR" });

// REAL-input specs for the fleet RCA inbox, against the DEPLOYED frontend and
// API. These exist because the API half shipped BEFORE the UI half in an
// earlier round: ?kind and ?cursor worked on the handler and the client had no
// way to send either, so the defect they fix was still live for the operator
// while every unit test was green. The assertions below are therefore on the
// REQUEST the page actually issues and on the response field it actually
// reads, not on the handler, which unit tests already cover.

/** Query params of the next GET /api/tasks the page issues. */
async function nextTasksQuery(
  page: import("@playwright/test").Page,
  act: () => Promise<unknown>,
) {
  const [req] = await Promise.all([
    page.waitForRequest(
      (r) => r.url().includes("/api/tasks") && !r.url().includes("/stats"),
    ),
    act(),
  ]);
  return new URL(req.url()).searchParams;
}

const scopeSelect = (page: import("@playwright/test").Page) =>
  page.locator("main select").filter({ hasText: "원인 분석만" });

test("RCA 받은함이 전체 클러스터 범위로 렌더된다", async ({ page }) => {
  await page.goto("/tasks");
  await expect(page.locator("main h1")).toHaveText(/RCA 받은함/);
  await expect(
    page.getByText(/Application error|Minified React error/),
  ).toHaveCount(0);

  // The scope control is the answer to "I had to open every cluster one by
  // one", so it must default to every cluster rather than the global
  // selection.
  await expect(page.getByText("모든 클러스터").first()).toBeVisible();

  // Freshness is stated in words, never as a bare number that could read as a
  // count of unread items.
  await expect(
    page.getByText(/첫 방문|새 리포트 없음|\d+건 신규|\d+건 이상 신규/).first(),
  ).toBeVisible();
});

test("종류 범위를 바꾸면 요청에 kind가 실제로 실린다", async ({ page }) => {
  await page.goto("/tasks");
  const select = scopeSelect(page);
  await expect(select).toBeVisible();

  // Default: RCA only. A digest carries status "done" exactly like a report,
  // so without this the newest page can be all digests and no reports.
  await expect(select).toHaveValue("rca");

  const toReport = await nextTasksQuery(page, () =>
    select.selectOption("report"),
  );
  expect(toReport.get("kind")).toBe("scheduled_report");

  const toAll = await nextTasksQuery(page, () => select.selectOption("all"));
  expect(toAll.get("kind")).toBeNull();

  const backToRca = await nextTasksQuery(page, () =>
    select.selectOption("rca"),
  );
  expect(backToRca.get("kind")).toBe("auto_rca,manual_rca");
});

test("이전 기록이 남아 있으면 더 보기가 커서로 다음 페이지를 가져온다", async ({
  page,
}) => {
  await page.goto("/tasks");
  await expect(page.locator("main h1")).toHaveText(/RCA 받은함/);

  const more = page.getByRole("button", { name: "더 보기" });
  // The fleet may legitimately fit on one page. Skipping is honest; asserting
  // on a button that should not exist would be worse.
  if ((await more.count()) === 0) {
    test.skip(
      true,
      "이 배포의 받은함이 한 페이지에 들어갑니다 (next_cursor 없음)",
    );
  }

  const before = await page.locator("main [data-task-row]").count();
  const q = await nextTasksQuery(page, () => more.click());
  expect(q.get("cursor"), "더 보기는 커서를 보내야 한다").toBeTruthy();
  // Appends rather than replaces: paging back through history must not throw
  // away what is already on screen.
  await expect
    .poll(() => page.locator("main [data-task-row]").count())
    .toBeGreaterThan(before);
});

test("예약 리포트는 유력 가설로 표시되지 않는다", async ({ page }) => {
  await page.goto("/tasks");
  const select = scopeSelect(page);
  await expect(select).toBeVisible();
  await select.selectOption("report");

  // Scoped to the row list. page.getByText("예약 리포트") also matches the
  // scope select's own <option> labels, which are raw Korean and unclickable,
  // so the unscoped version fails on visibility rather than on the claim.
  const rows = page
    .locator("main [data-task-row]")
    .filter({ hasText: "예약 리포트" });
  if ((await rows.count()) === 0) {
    test.skip(true, "이 배포에 예약 리포트 행이 없습니다");
  }

  // A digest has no ranked candidate, so labelling it as the leading
  // hypothesis, and attaching the ranking caveat, would claim a certainty that
  // no ranking ever produced.
  await rows.first().click();
  await expect(page.getByText("유력 가설")).toHaveCount(0);
  await expect(
    page.getByText(/순위는 조사 우선순위이며 확정된 원인이 아닙니다/),
  ).toHaveCount(0);
  await expect(
    page.getByText(/정기 점검 결과이며 원인 분석이 아닙니다/).first(),
  ).toBeVisible();
});

// Both directions of the locale switch. Nested describes so each inherits the
// project's baseURL and storageState: a hand-rolled browser.newContext() gets
// neither, so page.goto("/tasks") would have no origin to resolve against.
//
// This is the one failure mode the Korean-keyed table can have without
// failing tsc, the build or i18n-check: the translations are present and
// correct, and the page simply never switches. A one-directional assertion
// passes against a hardcoded title, so both are pinned.
test.describe("UI 언어는 브라우저 로케일을 따른다", () => {
  test.describe("한국어 브라우저", () => {
    test.use({ locale: "ko-KR" });
    test("한글 제목이 렌더된다", async ({ page }) => {
      await page.goto("/tasks");
      await expect(page.locator("main h1")).toHaveText(/RCA 받은함/);
    });
  });

  test.describe("영어 브라우저", () => {
    test.use({ locale: "en-US" });
    test("영문 제목이 렌더된다", async ({ page }) => {
      await page.goto("/tasks");
      await expect(page.locator("main h1")).toHaveText(/RCA inbox/);
    });
  });
});
