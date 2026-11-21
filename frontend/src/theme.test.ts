import { describe, expect, it } from "vitest";
import { STATUS_TOKENS, TOKEN_BACKGROUNDS, TOKEN_VALUES, contrast, tc } from "./theme";

describe("status tokens", () => {
  it("UI_REFRESH_1_every_token_has_a_value_in_both_schemes", () => {
    for (const scheme of ["light", "dark"] as const) {
      for (const t of STATUS_TOKENS) expect(TOKEN_VALUES[scheme][t]).toMatch(/^#[0-9a-f]{6}$/);
    }
    expect(tc("diffRemoved")).toBe("var(--rifferr-color-diff-removed)");
  });

  it("UI_REFRESH_2_tokens_reach_wcag_aa_on_card_backgrounds", () => {
    for (const scheme of ["light", "dark"] as const) {
      for (const bg of TOKEN_BACKGROUNDS[scheme]) {
        for (const t of STATUS_TOKENS) {
          const ratio = contrast(TOKEN_VALUES[scheme][t], bg);
          expect(ratio, `${scheme} ${t} on ${bg}`).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
  });

  it("contrast matches known WCAG values", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 1);
    expect(contrast("#777777", "#ffffff")).toBeCloseTo(4.48, 1);
  });
});
