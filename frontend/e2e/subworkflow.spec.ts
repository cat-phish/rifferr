import { expect, test } from "@playwright/test";

test("SUBWORKFLOW_13 the trust prompt lists the called file and trusts both", async ({ page }) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  await page.goto(`/auth/exchange?token=${token}`);

  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("main").getByRole("link", { name: "call-flow" }).click();

  const review = page.getByTestId("review");
  const called = review.getByTestId("called-call-shared.yaml");
  await expect(called).toContainText("Called workflow call-shared.yaml");
  await expect(called).toContainText("hello {{ vars.who }}");
  // SUBWORKFLOW-14: the called shell step is in the report, labelled with its file.
  await expect(review.getByTestId("safety")).toContainText("[greet/say]");
  await expect(review.getByTestId("safety")).toContainText("in call-shared.yaml");
  await expect(page.getByRole("button", { name: "Start run" })).toBeDisabled();
  await review.getByRole("button", { name: "Trust this workflow and 1 called file" }).click();
  await expect(review).toBeHidden();

  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);
  // SUBWORKFLOW-10: called steps are grouped under the call, with the workflow's name.
  await expect(page.getByTestId("call-greet")).toContainText("Shared greeting");
  await expect(page.getByTestId("step-greet/say")).toContainText("succeeded", { timeout: 30_000 });
  await expect(page.getByTestId("step-done")).toContainText("succeeded");
  await expect(page.getByTestId("log")).toContainText("hello e2e");
});
