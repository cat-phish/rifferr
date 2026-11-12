import { expect, test } from "@playwright/test";

test("UI_SHELL_13 two-step workflow: sign in, review, trust, start, watch the log", async ({
  page,
}) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  const inbox = process.env.RIFFERR_E2E_INBOX ?? "";

  // Sign in the way `rifferr serve` links to: the exchange sets the cookie and redirects.
  await page.goto(`/auth/exchange?token=${token}`);
  await expect(page).toHaveURL(/\/$/);
  expect(page.url()).not.toContain("token");
  await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();

  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("link", { name: "two-step" }).click();

  // Import review: YAML, hash, safety report, then Trust.
  const review = page.getByTestId("review");
  await expect(review).toContainText("beets.import");
  await expect(review.getByTestId("safety")).toContainText("command");
  await expect(page.getByRole("button", { name: "Start run" })).toBeDisabled();
  await review.getByRole("button", { name: "Trust this workflow" }).click();
  await expect(review).toBeHidden();

  await page.getByRole("textbox", { name: /inbox/ }).fill(inbox);
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);

  await expect(page.getByTestId("step-list-files")).toContainText("succeeded", { timeout: 30_000 });
  await expect(page.getByTestId("step-import")).toContainText("succeeded");
  const log = page.getByTestId("log");
  await expect(log).toContainText("[import] imported");
  await expect(log).toContainText("[list-files]");
  await expect(log).toContainText("First.flac");

  // Survives a reload (SPA fallback) and lists the run.
  await page.reload();
  await expect(page.getByTestId("step-list-files")).toContainText("succeeded");
  await page.getByRole("navigation").getByRole("link", { name: "Runs" }).click();
  await expect(page.getByTestId("runs")).toContainText("succeeded");
});
