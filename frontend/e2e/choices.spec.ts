import { expect, test } from "@playwright/test";

test("TAGS_CHOOSE_12 choose a non-suggested option, enter a custom value, undo the run", async ({
  page,
}) => {
  await page.goto(`/auth/exchange?token=${process.env.RIFFERR_E2E_TOKEN ?? ""}`);
  await page
    .getByRole("navigation")
    .getByRole("link", { name: /^Workflows/ })
    .first()
    .click();
  await page.getByRole("main").getByRole("link", { name: "Edit workflows" }).click();
  await page.getByRole("main").getByRole("link", { name: "choose-flow" }).click();
  await page.getByRole("button", { name: "Trust this workflow" }).click();
  await page
    .getByRole("textbox", { name: /inbox/ })
    .fill(process.env.RIFFERR_E2E_CHOICES_INBOX ?? "");
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);
  const runUrl = page.url();
  const waiting = page.getByTestId("waiting-review");
  await expect(waiting).toBeVisible({ timeout: 30_000 });
  await waiting.getByRole("link").click();

  // The album kind: Split is suggested; choose Collaboration instead.
  const kind = page.locator('[data-testid=queue-item][data-kind="choice"]');
  await expect(kind).toContainText("Choice", { timeout: 15_000 });
  await expect(kind.getByTestId("choice-shown")).toContainText("A & B");
  const split = kind.getByTestId("choice-option").filter({ hasText: "Split" });
  await expect(split).toContainText("suggested");
  await expect(split.getByRole("radio")).toBeChecked();
  const collab = kind.getByTestId("choice-option").filter({ hasText: "Collaboration" });
  await expect(collab).toContainText("albumartist: A & B → Collaboration");
  await collab.getByRole("radio").check();
  await kind.getByRole("button", { name: "Apply" }).click();

  // Then the release date, typed in.
  await page.getByRole("tab", { name: /dates/ }).click({ timeout: 30_000 });
  const dates = page.locator('[data-testid=queue-item][data-kind="choice"]');
  await expect(dates.getByTestId("choice-custom")).toBeVisible({ timeout: 30_000 });
  await dates.getByLabel("releasedate").fill("2004-03-22");
  await dates.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByTestId("queue-empty")).toBeVisible({ timeout: 30_000 });

  // Undo the run: both choices are reverted.
  await page.goto(runUrl);
  await expect(page.getByTestId("step-dates")).toContainText("succeeded", { timeout: 30_000 });
  await page.getByRole("button", { name: "Undo this run" }).click();
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(page.getByTestId("undo-report")).toContainText("Undone", { timeout: 30_000 });
});
