import { describe, expect, it } from "vitest";
import { diffText, newOf, oldOf } from "./diff";

describe("diffText", () => {
  it("UI_REFRESH_4_removed_suffix", () => {
    expect(diffText("Intro (Remastered 2011)", "Intro")).toEqual([
      { kind: "same", text: "Intro" },
      { kind: "removed", text: " (Remastered 2011)" },
    ]);
  });

  it("UI_REFRESH_4_character_change_inside_a_word", () => {
    expect(diffText("Colour", "Color")).toEqual([
      { kind: "same", text: "Colo" },
      { kind: "removed", text: "u" },
      { kind: "same", text: "r" },
    ]);
  });

  it("UI_REFRESH_4_mostly_different_values_diff_by_word", () => {
    const segs = diffText("The Wall", "Animals live");
    for (const s of segs) if (s.kind !== "same") expect(s.text).not.toMatch(/^[a-z]$/i);
    expect(oldOf(segs)).toBe("The Wall");
    expect(newOf(segs)).toBe("Animals live");
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
