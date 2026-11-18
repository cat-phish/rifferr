/// <reference lib="dom" />
import { type Locator, type Page, expect, test } from "@playwright/test";

/** A PNG header with a size: enough for Rifferr's checks (magic bytes, IHDR). */
function pngHeader(width: number, height: number): number[] {
  const be = (n: number) => [(n >>> 24) & 0xff, (n >>> 16) & 0xff, (n >>> 8) & 0xff, n & 0xff];
  return [
    0x89,
    0x50,
    0x4e,
    0x47,
    0x0d,
    0x0a,
    0x1a,
    0x0a,
    ...be(13),
    0x49,
    0x48,
    0x44,
    0x52,
    ...be(width),
    ...be(height),
    8,
    2,
    0,
    0,
    0,
    0,
    0,
    0,
    0, // CRC (not checked)
    0,
    0,
    0,
    0,
    0x49,
    0x45,
    0x4e,
    0x44,
  ];
}

/** Paste an image into the picker, as Ctrl+V with an image on the clipboard does. */
async function pasteImage(picker: Locator, bytes: number[]): Promise<void> {
  await picker.evaluate((el, data) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([new Uint8Array(data)], "pasted.png", { type: "image/png" }));
    el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true }));
  }, bytes);
}

const order = (picker: Locator) =>
  picker
    .getByTestId("cover-candidate")
    .evaluateAll((cards) => cards.map((c) => (c as HTMLElement).dataset.provider ?? ""));

async function signIn(page: Page): Promise<void> {
  await page.goto(`/auth/exchange?token=${process.env.RIFFERR_E2E_TOKEN ?? ""}`);
}

test("COVER_PICKER_2 COVER_PICKER_9 COVER_PICKER_14 COVER_PICKER_17 library: change cover, paste, weights, undo", async ({
  page,
}) => {
  await signIn(page);
  await page.getByRole("navigation").getByRole("link", { name: "Library" }).click();
  await page.getByRole("textbox", { name: "Query" }).fill("'album:Cover Sessions'");
  const rows = page.getByTestId("grid-row");
  await expect(rows).toHaveCount(1, { timeout: 30_000 });
  await expect(rows.first().getByTestId("art-cell")).toBeVisible();
  await rows.first().getByRole("button", { name: "Change cover of Cover Sessions" }).click();

  // Album mode searches at once; the local provider finds the folder's image.
  const picker = page.getByTestId("cover-picker");
  await expect(picker.getByRole("textbox", { name: "Album" })).toHaveValue("Cover Sessions", {
    timeout: 30_000,
  });
  const local = picker.locator('[data-testid=cover-candidate][data-provider="local"]');
  await expect(local).toContainText("800×800", { timeout: 30_000 });
  await expect(local.getByTestId("score")).toHaveText("score 0.80");

  // A pasted image joins the grid and ranks first at 1000×1000.
  await pasteImage(picker, pngHeader(1000, 1000));
  const upload = picker.locator('[data-testid=cover-candidate][data-provider="upload"]');
  await expect(upload).toContainText("1000×1000", { timeout: 15_000 });
  await expect.poll(() => order(picker)).toEqual(["upload", "local"]);

  // A weight change re-ranks at once, without a new search, and saves to the profile.
  const slider = picker
    .locator('[data-testid=provider-row][data-name="local"]')
    .getByRole("slider");
  await slider.focus();
  await slider.press("End");
  await expect(local.getByTestId("score")).toHaveText("score 8.00");
  await expect.poll(() => order(picker)).toEqual(["local", "upload"]);
  await picker.getByRole("button", { name: "Save as defaults" }).click();
  await expect(picker.getByTestId("saved")).toBeVisible();

  // Choosing applies a manual ChangeSet, which Recent edits can undo.
  await local.getByRole("button", { name: "Use this cover" }).click();
  await expect(picker.getByTestId("decision-sent")).toContainText("Cover set", {
    timeout: 30_000,
  });
  await page.keyboard.press("Escape");
  const edit = page.getByTestId("recent-edit").first();
  await expect(edit).toContainText("art_sha256", { timeout: 15_000 });
  await expect(edit).toContainText("applied");
  await edit.getByRole("button", { name: "Undo" }).click();
  await expect(page.getByTestId("undo-result").first()).toContainText("Undone.", {
    timeout: 30_000,
  });
  await expect(edit).toContainText("undone");
});

test("COVER_PICKER_1 COVER_PICKER_13 COVER_PICKER_17 review queue: choose a pasted cover", async ({
  page,
}) => {
  await signIn(page);
  await page.getByRole("navigation").getByRole("link", { name: "Workflows" }).click();
  await page.getByRole("link", { name: "cover-flow" }).click();
  await page.getByRole("button", { name: "Trust this workflow" }).click();
  await page
    .getByRole("textbox", { name: /inbox/ })
    .fill(process.env.RIFFERR_E2E_COVERS_INBOX ?? "");
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9A-Z]{26}\?profile=main$/);
  const runUrl = page.url();
  const waiting = page.getByTestId("waiting-review");
  await expect(waiting).toBeVisible({ timeout: 30_000 });
  await waiting.getByRole("link").click();

  const item = page.locator('[data-testid=queue-item][data-kind="cover"]');
  await expect(item).toContainText("Cover choice", { timeout: 15_000 });
  const picker = item.getByTestId("cover-picker");
  await expect(picker).toContainText("Cover Review");
  await pasteImage(picker, pngHeader(1200, 1200));
  const upload = picker.locator('[data-testid=cover-candidate][data-provider="upload"]');
  await expect(upload).toContainText("1200×1200", { timeout: 15_000 });
  await upload.getByRole("button", { name: "Use this cover" }).click();
  await expect(page.getByTestId("queue-empty")).toBeVisible({ timeout: 30_000 });

  await page.goto(runUrl);
  await expect(page.getByTestId("step-art")).toContainText("succeeded", { timeout: 30_000 });
});
