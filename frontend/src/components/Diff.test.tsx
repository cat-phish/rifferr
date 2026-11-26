import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { Diff } from "./Diff";

function show(old: string, next: string, side?: "both" | "old" | "new") {
  render(
    <MantineProvider env="test">
      <span data-testid="cell">
        <Diff old={old} new={next} side={side} />
      </span>
    </MantineProvider>,
  );
  return screen.getByTestId("cell");
}

test("UI_REFRESH_3_removed_text_is_struck_through_not_only_red", () => {
  const cell = show("Intro (Live)", "Intro");
  const removed = cell.querySelector('[data-seg="removed"]') as HTMLElement;
  expect(removed).toHaveTextContent("(Live)");
  expect(removed.style.textDecoration).toBe("line-through");
  expect(removed.style.color).toContain("--rifferr-color-diff-removed");
});

test("UI_REFRESH_5_an_unchanged_value_is_dimmed", () => {
  const cell = show("Same", "Same");
  expect(cell.querySelector('[data-diff="same"]')).toHaveTextContent("Same");
  expect(cell.querySelector("[data-seg]")).toBeNull();
});

test("UI_REFRESH_4_side_by_side_columns_show_their_own_changes", () => {
  const old = show("01 First.flac", "01. First.flac", "old");
  expect(old).toHaveTextContent("01 First.flac");
  expect(old.querySelector('[data-seg="added"]')).toBeNull();
});

test("REVIEW_4 values with nothing in common show apart as old → new", () => {
  const cell = show("0", "1982");
  expect(cell).toHaveTextContent("0 → 1982");
  expect(cell.querySelector('[data-seg="removed"]')).toHaveTextContent(/^0$/);
  expect(cell.querySelector('[data-seg="added"]')).toHaveTextContent(/^1982$/);
});
