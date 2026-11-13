import { expect, test } from "@playwright/test";

test("TAG_GRID_15 library grid: query, expand, edit, check, apply, undo", async ({ page }) => {
  const token = process.env.RIFFERR_E2E_TOKEN ?? "";
  await page.goto(`/auth/exchange?token=${token}`);
  await page.getByRole("navigation").getByRole("link", { name: "Library" }).click();
  await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();

  // The query runs in the read-only worker against the real beets library.
  await page.getByRole("textbox", { name: "Query" }).fill("'album:Preview Sessions'");
  const rows = page.getByTestId("grid-row");
  await expect(rows).toHaveCount(1, { timeout: 30_000 });
  const album = rows.first();
  await expect(album).toContainText("Preview Sessions");

  await album.getByRole("button", { name: "Expand" }).click();
  await expect(rows.nth(1)).toHaveAttribute("data-row", /^item:/);

  const year = album.locator('[data-testid=cell][data-field="year"]');
  const before = (await year.textContent())?.trim() ?? "";
  const wanted = before === "1987" ? "1988" : "1987";
  await year.dblclick();
  const input = page.getByRole("textbox", { name: "year value" });
  await input.fill(wanted);
  await input.press("Enter");
  await expect(year).toHaveAttribute("data-pending", "true");
  await expect(year).toContainText(`→ ${wanted}`);

  // Checked by the server, then applied through the journal.
  const apply = page.getByRole("button", { name: "Apply" });
  await expect(apply).toBeEnabled({ timeout: 15_000 });
  await apply.click();
  await expect(page.getByTestId("notice")).toContainText("Edits applied.", { timeout: 30_000 });
  await expect(year).toHaveText(wanted, { timeout: 15_000 });
  await expect(year).not.toHaveAttribute("data-pending", "true");

  const edit = page.getByTestId("recent-edit").first();
  await expect(edit).toContainText("year");
  await expect(edit).toContainText("applied");
  await edit.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByTestId("undo-result").first()).toContainText("Undone.", {
    timeout: 30_000,
  });
  await expect(year).toHaveText(before, { timeout: 15_000 });
  await expect(edit).toContainText("undone");
});
