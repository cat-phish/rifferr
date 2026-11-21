import { type CSSVariablesResolver, createTheme } from "@mantine/core";

/**
 * Status colors (F-UI-REFRESH UI-REFRESH-1), modelled on beets' `ui.colors`:
 * green for a strong match or an unchanged value, yellow for a medium match or a
 * warning, red for a weak match, an error or a value that changes.
 */
export const STATUS_TOKENS = [
  "success",
  "warning",
  "danger",
  "changed",
  "diffRemoved",
  "diffAdded",
] as const;
export type StatusToken = (typeof STATUS_TOKENS)[number];

/** Token values per scheme. Each reaches WCAG AA on the card background (UI-REFRESH-2). */
export const TOKEN_VALUES: Record<"light" | "dark", Record<StatusToken, string>> = {
  light: {
    success: "#1e7a34",
    warning: "#8a5800",
    danger: "#c42727",
    changed: "#c42727",
    diffRemoved: "#c42727",
    diffAdded: "#1e7a34",
  },
  dark: {
    success: "#69db7c",
    warning: "#ffd43b",
    danger: "#ff8787",
    changed: "#ff8787",
    diffRemoved: "#ff8787",
    diffAdded: "#69db7c",
  },
};

/** Backgrounds the tokens are checked against: body and card/paper per scheme. */
export const TOKEN_BACKGROUNDS: Record<"light" | "dark", string[]> = {
  light: ["#ffffff", "#f8f9fa"],
  dark: ["#242424", "#2e2e2e"],
};

function varName(token: StatusToken): string {
  return `--rifferr-color-${token.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
}

/** CSS value for a token, for `c={tc("changed")}` or `style={{ color: tc(…) }}`. */
export function tc(token: StatusToken): string {
  return `var(${varName(token)})`;
}

function schemeVars(scheme: "light" | "dark"): Record<string, string> {
  return Object.fromEntries(STATUS_TOKENS.map((t) => [varName(t), TOKEN_VALUES[scheme][t]]));
}

export const cssVariablesResolver: CSSVariablesResolver = () => ({
  variables: {},
  light: schemeVars("light"),
  dark: schemeVars("dark"),
});

export const theme = createTheme({});

function channel(c: number): number {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const n = Number.parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG contrast ratio of two `#rrggbb` colors. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}
