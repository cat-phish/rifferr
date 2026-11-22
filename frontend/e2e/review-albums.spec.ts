import { expect, test } from "@playwright/test";

test("UI_REFRESH_17 two albums wait in the import section; a decided one disappears at once", async ({
  page,
}) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  const inbox = process.env.RIFFERR_E2E_REFRESH_INBOX ?? "";
  await page.goto(`/auth/exchange?token=${token}`);

  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("link", { name: "import-flow" }).click();
  // imports.spec.ts may have trusted it already.
  const trust = page.getByRole("button", { name: "Trust this workflow" });
  if (await trust.isVisible()) await trust.click();
  await page.getByRole("textbox", { name: /inbox/ }).fill(inbox);
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);
  const runId = /\/runs\/([0-9A-Z]{26})/.exec(page.url())?.[1] ?? "";
  await expect(page.getByTestId("waiting-review")).toBeVisible({ timeout: 30_000 });

  // Neither album has a candidate: both wait in the import step's section, oldest first,
  // each with its album label (REVIEW-QUEUE-11, UI-REFRESH-13).
  // Review lists each workflow (UI-REFRESH-22); one tab per stage (UI-REFRESH-13).
  await page.getByTestId("review-workflow-link").filter({ hasText: "import-flow" }).click();
  await page
    .getByRole("tab", { name: /import/ })
    .first()
    .click();
  const ofRun = page
    .locator("[data-testid=queue-item]")
    .filter({ has: page.locator(`a[href^="/runs/${runId}"]`) });
  const flac = ofRun.filter({ hasText: "album-flac" });
  const mp3 = ofRun.filter({ hasText: "single-mp3" });
  await expect(flac).toBeVisible({ timeout: 30_000 });
  await expect(mp3).toBeVisible();
  const section = page.getByTestId("step-section").filter({ has: flac });
  await expect(section.getByTestId("step-label")).toHaveText(/import$/);
  await expect(section).toContainText("single-mp3");

  // A candidate found by ID gets a match badge colored by beets' thresholds (UI-REFRESH-6).
  await flac.getByPlaceholder("release ID or URL").fill("e2e-release");
  await flac.getByRole("button", { name: "Search by ID" }).click();
  const found = flac.locator('[data-candidate="RifferrFixture:e2e-release"]');
  await expect(found).toContainText("similarity", { timeout: 30_000 });
  await expect(found.locator("[data-token]")).not.toHaveAttribute("data-token", "none");

  // Importing one album makes its item disappear at once; nothing takes its place
  // (UI-REFRESH-20), and the other album still waits.
  await found.getByRole("radio").check();
  await flac.getByRole("button", { name: "Import this match" }).click();
  await expect(flac).toBeHidden({ timeout: 5_000 });
  await expect(page.getByText("Working")).toHaveCount(0);
  await expect(mp3).toBeVisible();

  // Leave the queue empty for the other tests.
  await mp3.getByRole("button", { name: "Skip" }).click();
  await expect(ofRun).toHaveCount(0, { timeout: 30_000 });
});
