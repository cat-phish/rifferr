import { existsSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";

test("LIBRARY_MOVE_16 a move waits for review with old and new folders; accepting moves the album", async ({
  page,
}) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  const inbox = process.env.RIFFERR_E2E_MOVES_INBOX ?? "";
  const library = process.env.RIFFERR_E2E_MOVED_LIBRARY ?? "";
  const src = join(inbox, "Move Sessions");
  const dst = join(library, "Move Artist", "Move Sessions");
  await page.goto(`/auth/exchange?token=${token}`);

  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("link", { name: "move-flow" }).click();
  await page.getByRole("button", { name: "Trust this workflow" }).click();
  await page.getByRole("textbox", { name: /inbox/ }).fill(inbox);
  await page.getByRole("textbox", { name: /library/ }).fill(library);
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);

  // Nothing moves before review (LIBRARY-MOVE-1: apply defaults to review).
  await expect(page.getByTestId("waiting-review")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("waiting-review").getByRole("link").first().click();
  // The whole queue item: its actions sit in the header row (UI-REFRESH-10).
  const item = page
    .locator("[data-testid=queue-item]")
    .filter({ has: page.getByTestId("file-changeset") });
  await expect(item.getByTestId("old-folder")).toHaveText(src);
  await expect(item.getByTestId("new-folder")).toHaveText(dst);
  await expect(item.getByTestId("file-stamp")).toContainText("date_added");
  const rows = item.getByTestId("file-effect");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0).getByTestId("old-name")).toHaveText("01 First.flac");
  await expect(rows.nth(0).getByTestId("new-name")).toHaveText("01. First.flac");
  await expect(item.locator('[data-kind="artifact"]').getByTestId("new-name")).toHaveText(
    "rip.log",
  );
  expect(existsSync(join(src, "01 First.flac"))).toBe(true);

  await item.getByRole("button", { name: "Move album" }).click();
  await expect(item).toBeHidden({ timeout: 30_000 });
  await page.goBack();
  await expect(page.getByTestId("waiting-review")).toBeHidden({ timeout: 30_000 });
  expect(existsSync(join(dst, "01. First.flac"))).toBe(true);
  expect(existsSync(join(dst, "rip.log"))).toBe(true);
  expect(existsSync(join(src, "01 First.flac"))).toBe(false);
});
