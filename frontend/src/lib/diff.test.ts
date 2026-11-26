import { describe, expect, it } from "vitest";
import { diffText, newOf, oldOf } from "./diff";

describe("diffText", () => {
  it("UI_REFRESH_4_removed_suffix", () => {
    expect(diffText("Intro (Remastered 2011)", "Intro")).toEqual([
      { kind: "same", text: "Intro" },
      { kind: "removed", text: " (Remastered 2011)" },
    ]);
  });

  it("UI_REFRESH_4_a_changed_word_is_marked_whole", () => {
    expect(diffText("Colour", "Color")).toEqual([
      { kind: "removed", text: "Colour" },
      { kind: "added", text: "Color" },
    ]);
  });

  it("UI_REFRESH_4_a_path_diffs_by_folder_name", () => {
    expect(diffText("/r/staging/Lady Gaga/MAYHEM", "/r/library/Lady Gaga/MAYHEM")).toEqual([
      { kind: "same", text: "/r/" },
      { kind: "removed", text: "staging" },
      { kind: "added", text: "library" },
      { kind: "same", text: "/Lady Gaga/MAYHEM" },
    ]);
  });

  it("UI_REFRESH_4_punctuation_is_its_own_word", () => {
    expect(diffText("Let's Have a War - Waste", "Let's Have a War ／ Waste")).toEqual([
      { kind: "same", text: "Let's Have a War " },
      { kind: "removed", text: "-" },
      { kind: "added", text: "／" },
      { kind: "same", text: " Waste" },
    ]);
  });

  it("UI_REFRESH_4_removed_comes_before_added", () => {
    const segs = diffText("1999", "2001");
    const kinds = segs.map((s) => s.kind).filter((k) => k !== "same");
    expect(kinds.indexOf("removed")).toBeLessThan(kinds.indexOf("added"));
  });

  it("equal and empty values", () => {
    expect(diffText("a", "a")).toEqual([{ kind: "same", text: "a" }]);
    expect(diffText("", "")).toEqual([]);
    expect(diffText("", "new")).toEqual([{ kind: "added", text: "new" }]);
  });

  it("UI_REFRESH_4_round_trips_old_and_new", () => {
    // Table of awkward pairs: unicode, whitespace, repeats.
    const pairs: [string, string][] = [
      ["Björk", "Bjork"],
      ["a  b", "a b"],
      ["aaaa", "aa"],
      ["", "x"],
      ["Sigur Rós – ( )", "Sigur Ros - ()"],
      ["🎵 Track", "Track 🎵"],
      ["x".repeat(600), "y".repeat(600)],
    ];
    for (const [a, b] of pairs) {
      const segs = diffText(a, b);
      expect(oldOf(segs)).toBe(a);
      expect(newOf(segs)).toBe(b);
    }
  });
});
