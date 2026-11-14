import { cpSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";

test("TRIGGERS_9 a folder dropped into a watched folder starts a run", async ({ page }) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  const watched = process.env.RIFFERR_E2E_WATCHED ?? "";
  const fixture = process.env.RIFFERR_E2E_FIXTURE ?? "";
  await page.goto(`/auth/exchange?token=${token}`);

  // Untrusted workflows never arm (TRIGGERS-2); trusting arms the watch at once.
  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("link", { name: "watch-flow" }).click();
  const watch = page.getByTestId("trigger-watch");
  await expect(watch).toContainText("disabled");
  await expect(watch).toContainText("untrusted");
  await page.getByRole("button", { name: "Trust this workflow" }).click();
  await expect(watch).toContainText("armed", { timeout: 15_000 });
  await expect(watch).toContainText(watched);

  // Drop an album folder in; once it settles a run starts with it as the input.
  cpSync(fixture, join(watched, "Dropped Album"), { recursive: true });
  await page.getByRole("navigation").getByRole("link", { name: "Runs" }).click();
  const row = page.getByTestId("runs").getByRole("row").filter({ hasText: "watch-flow" });
  await expect(row).toHaveCount(1, { timeout: 30_000 });
  await expect(row).toContainText("watch");
  await expect(row).toContainText("Dropped Album");
  await row.getByRole("link", { name: "watch-flow" }).click();
  await expect(page.getByTestId("run-trigger")).toContainText(join(watched, "Dropped Album"));
  await expect(page.getByTestId("step-list")).toContainText("succeeded", { timeout: 30_000 });
});
