import { existsSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";

test("FILES_CLEANUP_14 a cleanup waits for review; Delete removes the listed files", async ({
  page,
}) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  const inbox = process.env.RIFFERR_E2E_CLEANUP_INBOX ?? "";
  await page.goto(`/auth/exchange?token=${token}`);

  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("link", { name: "cleanup-flow" }).click();
  await page.getByRole("button", { name: "Trust this workflow" }).click();
  await page.getByRole("textbox", { name: /inbox/ }).fill(inbox);
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);

  // Nothing is deleted yet: the run waits for the cleanup decision (FILES-CLEANUP-14).
  await expect(page.getByTestId("waiting-review")).toBeVisible({ timeout: 30_000 });
  const lane = page.getByTestId("lanes").getByRole("row").filter({ hasText: "cleanup of" });
  await lane.getByRole("link", { name: "cleanup decision" }).click();

  const item = page.locator('[data-testid=queue-item][data-kind="cleanup"]');
  await expect(item.getByTestId("cleanup-path")).toContainText("cleanup-inbox");
  await expect(item.locator('[data-cls="delete"]')).toContainText("Album/info.nfo");
  await expect(item.locator('[data-cls="left"]')).toContainText("Album/notes.txt");
  expect(existsSync(join(inbox, "Album", "info.nfo"))).toBe(true);
  await item.getByRole("button", { name: "Delete" }).click();
  await expect(item).toBeHidden({ timeout: 30_000 });

  await page.goBack();
  await expect(page.getByTestId("waiting-review")).toBeHidden({ timeout: 30_000 });
  await expect(page.getByTestId("step-clean")).toContainText("succeeded");
  expect(existsSync(join(inbox, "Album", "info.nfo"))).toBe(false);
  expect(existsSync(join(inbox, "Album", "notes.txt"))).toBe(true);
});
