import { expect, test } from "@playwright/test";

test("IMPORT_UI_15 an unmatched album waits; search by ID, import, the lane goes on", async ({
  page,
}) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  const inbox = process.env.RIFFERR_E2E_IMPORTS_INBOX ?? "";
  await page.goto(`/auth/exchange?token=${token}`);

  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("link", { name: "import-flow" }).click();
  await page.getByRole("button", { name: "Trust this workflow" }).click();
  await page.getByRole("textbox", { name: /inbox/ }).fill(inbox);
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);

  // beets finds no candidate: the album's lane waits for an import decision (IMPORT-UI-16).
  await expect(page.getByTestId("waiting-review")).toBeVisible({ timeout: 30_000 });
  const lane = page.getByTestId("lanes").getByRole("row").filter({ hasText: "album-flac" });
  await expect(lane).toContainText("waiting");
  await lane.getByRole("link", { name: "import decision" }).click();

  const item = page.locator('[data-testid=queue-item][data-kind="import"]');
  await expect(item.getByTestId("import-path")).toContainText("album-flac");
  await expect(item.getByTestId("no-candidates")).toBeVisible();
  await item.getByPlaceholder("release ID or URL").fill("e2e-release");
  await item.getByRole("button", { name: "Search by ID" }).click();
  const found = item.locator('[data-candidate="RifferrFixture:e2e-release"]');
  await expect(found).toContainText("Found By ID", { timeout: 30_000 });
  await found.getByRole("radio").check();
  await expect(item.getByTestId("track-table")).toContainText("Second");
  await item.getByRole("button", { name: "Import this match" }).click();
  await expect(item).toBeHidden({ timeout: 30_000 });

  // The lane is renamed to the imported album and runs the rest of the workflow.
  await page.goBack();
  const imported = page
    .getByTestId("lanes")
    .getByRole("row")
    .filter({ hasText: "Fixture Artist - Found By ID" });
  await expect(imported).toContainText("done", { timeout: 30_000 });
  await expect(page.getByTestId("waiting-review")).toBeHidden();
  await expect(page.getByTestId("step-mark")).toContainText("succeeded");
});
