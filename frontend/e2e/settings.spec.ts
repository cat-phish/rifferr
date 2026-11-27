import { expect, test } from "@playwright/test";

test("SETTINGS_8 edit the AI section, save it, and test the connection", async ({ page }) => {
  await page.goto(`/auth/exchange?token=${process.env.RIFFERR_E2E_TOKEN ?? ""}`);
  await page.goto("/?profile=main");
  await page.getByRole("link", { name: "Settings" }).first().click();
  const ai = page.getByTestId("settings-ai");
  const stub = ai.locator('[data-testid=provider][data-name="stub"]');
  await expect(stub.getByLabel("Model")).toHaveValue("stub-1");
  await expect(stub.getByTestId("remote-notice")).toHaveCount(0); // localhost stub

  await stub.getByRole("button", { name: "Test connection" }).click();
  await expect(stub.getByTestId("test-result")).toHaveText("Connected: stub-1");

  // No OS keyring in the test environment: the page says how to use an environment variable.
  await expect(stub.getByTestId("no-keyring")).toBeVisible();

  const concurrency = ai.getByLabel("Requests at the same time");
  await concurrency.fill("3");
  await ai.getByRole("button", { name: "Save" }).click();
  await expect(ai.getByRole("button", { name: "Save" })).toBeDisabled();
  await page.reload();
  await expect(page.getByTestId("settings-ai").getByLabel("Requests at the same time")).toHaveValue(
    "3",
  );
});
