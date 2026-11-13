import { expect, test } from "@playwright/test";

test("REVIEW_QUEUE_18 review queue: partial accept, continue the gate, undo the run", async ({
  page,
}) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  const inbox = process.env.RIFFERR_E2E_INBOX ?? "";
  await page.goto(`/auth/exchange?token=${token}`);

  // Start the run: import, a tags.script step with apply: review, then a review gate.
  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("link", { name: "review-flow" }).click();
  // Every workflow file from outside Rifferr is reviewed and trusted first (SECURITY-12).
  await page.getByRole("button", { name: "Trust this workflow" }).click();
  await page.getByRole("textbox", { name: /inbox/ }).fill(inbox);
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);
  const runUrl = page.url();

  // The run waits for review and links to its items.
  const waiting = page.getByTestId("waiting-review");
  await expect(waiting).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("review-count")).toHaveText("1");
  await waiting.getByRole("link").click();
  await expect(page.getByRole("heading", { name: "Review" })).toBeVisible();

  // Partially accept the ChangeSet: the album suffix, not the disambiguation.
  const changeset = page.locator('[data-testid=queue-item][data-kind="changeset"]');
  await expect(changeset).toHaveCount(1);
  const album = changeset.locator('[data-testid=field-change][data-field="album"]');
  await expect(album.getByTestId("proposed")).toHaveText("Review Sessions (EP)");
  await expect(album.getByTestId("now")).toHaveText("Review Sessions", { timeout: 30_000 });
  await changeset.getByRole("checkbox", { name: /Accept albumdisambig/ }).uncheck();
  await changeset.getByRole("button", { name: "Accept selected" }).click();

  // The run resumes and stops at its review gate, which shows the folded change.
  const gate = page.locator('[data-testid=queue-item][data-kind="gate"]');
  await expect(gate).toBeVisible({ timeout: 30_000 });
  await expect(changeset).toHaveCount(0);
  await expect(gate).toContainText("Check the suffix");
  await expect(gate.getByTestId("gate-changes")).toContainText("Review Sessions (EP)");
  await expect(gate.getByTestId("gate-changes")).not.toContainText("albumdisambig");
  await gate.getByLabel("Note (optional)").fill("suffix looks right");
  await gate.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByTestId("queue-empty")).toBeVisible({ timeout: 30_000 });

  // The run finishes; undo it from its page.
  await page.goto(runUrl);
  await expect(page.getByTestId("step-check")).toContainText("succeeded", { timeout: 30_000 });
  await expect(page.getByTestId("waiting-review")).toBeHidden();
  await page.getByRole("button", { name: "Undo this run" }).click();
  await expect(page.getByTestId("undo-confirm")).toContainText("1 tag change");
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(page.getByTestId("undo-report")).toContainText("Undone: 1 change reverted.", {
    timeout: 30_000,
  });
  await expect(page.getByRole("button", { name: "Undo this run" })).toBeHidden();
});
