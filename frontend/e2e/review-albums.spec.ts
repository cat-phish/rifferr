import { expect, test } from "@playwright/test";

test("UI_REFRESH_17 two albums wait as album blocks; deciding one updates it live", async ({
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

  // Neither album has a candidate: both wait, each in its own block (UI-REFRESH-13, -14).
  await page
    .getByRole("navigation")
    .getByRole("link", { name: /Review/ })
    .click();
  const run = page.locator(`[data-testid=run-group][data-run="${runId}"]`);
  const flac = run.getByTestId("album-block").filter({ hasText: "album-flac" });
  const mp3 = run.getByTestId("album-block").filter({ hasText: "single-mp3" });
  await expect(flac.getByTestId("album-status")).toHaveText("waiting on you", { timeout: 30_000 });
  await expect(mp3.getByTestId("album-status")).toHaveText("waiting on you");
  await expect(run.getByTestId("lane-counts")).toContainText("2 waiting");

  // A candidate found by ID gets a match badge colored by beets' thresholds (UI-REFRESH-6).
  const item = flac.locator('[data-testid=queue-item][data-kind="import"]');
  await item.getByPlaceholder("release ID or URL").fill("e2e-release");
  await item.getByRole("button", { name: "Search by ID" }).click();
  const found = item.locator('[data-candidate="RifferrFixture:e2e-release"]');
  await expect(found).toContainText("similarity", { timeout: 30_000 });
  await expect(found.locator("[data-token]")).not.toHaveAttribute("data-token", "none");

  // Importing one album updates the run's blocks and counts without a reload.
  await found.getByRole("radio").check();
  await item.getByRole("button", { name: "Import this match" }).click();
  await expect(flac).toBeHidden({ timeout: 30_000 });
  await expect(run.getByTestId("lane-counts")).toContainText("1 waiting");
  await expect(run.getByTestId("lane-counts")).toContainText("1 done", { timeout: 30_000 });
  await expect(mp3.getByTestId("album-status")).toHaveText("waiting on you");

  // Leave the queue empty for the other tests.
  await mp3.getByRole("button", { name: "Skip" }).click();
  await expect(run).toBeHidden({ timeout: 30_000 });
});
