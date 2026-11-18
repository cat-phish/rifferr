import { cpSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";

test("DASHBOARD_17 inbox, run in progress, review from the dashboard, undo the import", async ({
  page,
}) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  const inbox = process.env.RIFFERR_E2E_DASHBOARD ?? "";
  const fixture = process.env.RIFFERR_E2E_FIXTURE ?? "";
  await page.goto(`/auth/exchange?token=${token}`);
  await page.getByRole("navigation").getByRole("link", { name: "Dashboard" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

  // A dropped folder shows in the inbox while it settles (DASHBOARD-1, -15).
  cpSync(fixture, join(inbox, "Dashboard Album"), { recursive: true });
  const folder = page.getByTestId("inbox-folder").filter({ hasText: "dashboard-flow" });
  const entry = folder.getByTestId("inbox-entry").filter({ hasText: "Dashboard Album" });
  await expect(entry).toHaveAttribute("data-state", /settling|waiting|running/, {
    timeout: 15_000,
  });

  // Once it settles the run imports it and waits for review (DASHBOARD-4).
  await expect(entry).toHaveAttribute("data-state", "waiting_review", { timeout: 60_000 });
  const run = page.getByTestId("active-run").filter({ hasText: "dashboard-flow" });
  await expect(run).toContainText("waiting_review");
  const runId = (await run.getAttribute("data-run")) ?? "";

  // The waiting gate is reached from Needs review (DASHBOARD-5).
  await page
    .getByTestId("needs-review")
    .getByTestId("review-oldest")
    .filter({ hasText: "Dashboard Sessions" })
    .getByRole("link")
    .click();
  const gate = page.locator(`[data-kind="gate"][data-focused="true"]`);
  await expect(gate).toContainText("Check the import");
  await gate.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(gate).toHaveCount(0, { timeout: 15_000 }); // decided: it leaves the queue

  // The run finishes; its import shows under Recent imports and is undone (DASHBOARD-6 .. -10).
  await page.getByRole("navigation").getByRole("link", { name: "Dashboard" }).click();
  const imported = page.locator(`[data-testid="recent-import"][data-run="${runId}"]`);
  await expect(imported).toContainText("succeeded", { timeout: 30_000 });
  await expect(imported).toContainText("Dashboard Sessions");
  await imported.getByRole("button", { name: "Undo import" }).click();
  await expect(page.getByTestId("undo-album")).toContainText(
    "its files are deleted from the library folder",
  );
  await page.getByRole("button", { name: "Remove 1" }).click();
  await expect(imported).toHaveCount(0, { timeout: 30_000 });
  await expect(entry).toHaveAttribute("data-state", "undone", { timeout: 15_000 });
  await expect(entry.getByRole("button", { name: "Import again" })).toBeVisible();
});
