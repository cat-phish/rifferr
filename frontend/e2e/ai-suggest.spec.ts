import { expect, test } from "@playwright/test";

test("AI_SUGGEST_12 Ask AI on a choice card, then choose a new value it suggested", async ({
  page,
}) => {
  await page.goto(`/auth/exchange?token=${process.env.RIFFERR_E2E_TOKEN ?? ""}`);
  await page.goto("/workflows/ai-flow?profile=main&tab=details");
  await page.getByRole("button", { name: "Trust this workflow" }).click();
  await page.getByRole("textbox", { name: /inbox/ }).fill(process.env.RIFFERR_E2E_AI_INBOX ?? "");
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);
  const waiting = page.getByTestId("waiting-review");
  await expect(waiting).toBeVisible({ timeout: 30_000 });
  await waiting.getByRole("link").click();

  const card = page.locator('[data-testid=queue-item][data-kind="choice"]');
  await expect(card.getByTestId("choice-description")).toContainText("Fast and loud.", {
    timeout: 15_000,
  });
  await expect(page.getByRole("button", { name: "Suggest with AI for all (1)" })).toBeVisible();

  // What will be sent, before anything is.
  await card.getByRole("button", { name: "Show what will be sent" }).click();
  const preview = page.getByTestId("ai-preview");
  await expect(preview).toContainText("stub (stub-1)");
  await expect(preview).toContainText("Album data:");
  await page.keyboard.press("Escape");

  await card.getByRole("button", { name: "Ask AI" }).click();
  const suggestions = card.getByTestId("ai-suggestion");
  await expect(suggestions).toHaveCount(2, { timeout: 15_000 });
  await expect(suggestions.nth(0)).toContainText("Punk80%");
  const street = suggestions.nth(1);
  await expect(street).toContainText("New");
  await expect(street.getByRole("radio")).not.toBeChecked();
  await street.getByRole("radio").check();
  await card.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByTestId("queue-empty")).toBeVisible({ timeout: 30_000 });
});
