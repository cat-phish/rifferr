import { expect, test } from "@playwright/test";

const UNGUARDED = `actions:
  - name: ep-suffix
    when: 'albumtype:ep'
    set: { album: '$album (EP)' }
`;
const GUARDED = `actions:
  - name: ep-suffix
    when: 'albumtype:ep ^album::[(]EP[)]$'
    set: { album: '$album (EP)' }
`;

test("TAGSCRIPT_22 script editor: live preview, idempotence highlight, save", async ({ page }) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  await page.goto(`/auth/exchange?token=${token}`);
  await page.getByRole("navigation").getByRole("link", { name: "Scripts" }).click();
  await expect(page.getByRole("heading", { name: "Tag scripts" })).toBeVisible();

  await page.getByRole("textbox", { name: "New script" }).fill("release-types");
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/scripts\/release-types\?profile=main$/);

  const editor = page.getByRole("textbox", { name: "Script" });
  await editor.fill(UNGUARDED);
  await page.getByRole("textbox", { name: "Query" }).fill("albumtype:ep");

  // The preview runs in the read-only worker against the real beets library.
  const grid = page.getByTestId("preview-grid");
  await expect(grid).toContainText("Preview Sessions (EP)", { timeout: 30_000 });
  const warning = page.getByTestId("idempotence");
  await expect(warning).toContainText("ep-suffix");
  await expect(warning).toContainText("Preview Sessions (EP) (EP)");
  await expect(grid.locator("[data-flagged=true]")).toHaveCount(1);
  await expect(grid.locator("[data-responsible=true]")).toContainText("ep-suffix");

  // A guard makes it idempotent; the warning goes away.
  await editor.fill(GUARDED);
  await expect(warning).toBeHidden({ timeout: 15_000 });
  await expect(page.getByTestId("preview")).toContainText("idempotent");
  await expect(grid).toContainText("Preview Sessions (EP)");

  // Broken YAML shows line and column.
  await editor.fill("actions:\n  - sett: {a: 1}\n");
  await expect(page.getByTestId("preview-validation")).toContainText("2:5");

  await editor.fill(GUARDED);
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Saved")).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Scripts" }).click();
  const list = page.getByTestId("scripts");
  await expect(list).toContainText("release-types");
  await expect(list).toContainText("valid");
});
