import { MantineProvider } from "@mantine/core";
import { render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { GateChanges } from "./GateChanges";

type S = components["schemas"];

test("REVIEW_4 a cover change shows the images, not their sha256", () => {
  const before = "a".repeat(64);
  const after = "b".repeat(64);
  const album: S["GateAlbum"] = {
    target: { scope: "album", id: "a1" },
    label: "A - B",
    imported_by_run: true,
    changes: [
      {
        target: { scope: "album", id: "a1" },
        field: "art_sha256",
        before,
        after,
        existed_before: true,
        steps: ["art", "art-size"],
      },
    ],
  };
  render(
    <MantineProvider env="test">
      <GateChanges album={album} profile="main" />
    </MantineProvider>,
  );
  const row = screen.getByTestId("gate-change-row");
  expect(within(row).getByTestId("gate-change-field")).toHaveTextContent("cover");
  expect(row).not.toHaveTextContent(after);
  const [old, next] = within(row).getAllByRole("img");
  expect(old).toHaveAttribute("src", `/api/profiles/main/art/${before}`);
  expect(next).toHaveAttribute("src", `/api/profiles/main/art/${after}`);
  expect(
    within(row)
      .getAllByTestId("gate-change-step")
      .map((b) => b.textContent),
  ).toEqual(["art", "art-size"]);
});
