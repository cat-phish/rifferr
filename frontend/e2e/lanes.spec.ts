import { expect, test } from "@playwright/test";

test("LANES_15 one album waits at a review gate while the other finishes", async ({ page }) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  const inbox = process.env.RIFFERR_E2E_LANES_INBOX ?? "";
  await page.goto(`/auth/exchange?token=${token}`);

  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("link", { name: "lanes-flow" }).click();
  await page.getByRole("button", { name: "Trust this workflow" }).click();
  await page.getByRole("textbox", { name: /inbox/ }).fill(inbox);
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);

  // The gate holds only "Fixture Single"; "Fixture Album" runs the last step and is done.
  await expect(page.getByTestId("waiting-review")).toBeVisible({ timeout: 30_000 });
  const lanes = page.getByTestId("lanes");
  const album = lanes.getByRole("row").filter({ hasText: "Fixture Album" });
  const single = lanes.getByRole("row").filter({ hasText: "Fixture Single" });
  await expect(album).toContainText("done");
  await expect(single).toContainText("waiting");
  await expect(single).toContainText("mark");

  // Its hold links to the gate in the review queue; continuing it lets the album finish.
  await single.getByRole("link", { name: "review gate" }).click();
  const gate = page.locator('[data-testid=queue-item][data-kind="gate"]');
  await expect(gate).toContainText("Check Fixture Single");
  await gate.getByRole("button", { name: "Continue" }).click();
  await page.goBack();
  await expect(single).toContainText("done", { timeout: 30_000 });
  await expect(page.getByTestId("waiting-review")).toBeHidden();
  await expect(page.getByTestId("step-mark")).toContainText("succeeded");
});
