import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig, devices } from "@playwright/test";

/** UI-SHELL-13: end to end against a real `rifferr serve` with a temp profile. */
export const E2E_PORT = Number(process.env.RIFFERR_E2E_PORT ?? "8799");

/**
 * The Chromium to run, or undefined for Playwright's own. Playwright's download won't start on
 * NixOS (it lacks glib, nss and others), so there the nixpkgs build is used: `nix build` keeps an
 * out-link in node_modules/.cache, which is a GC root and makes later runs instant.
 * `PLAYWRIGHT_CHROMIUM=/path/to/chromium` overrides both.
 */
function chromiumPath(): string | undefined {
  if (process.env.PLAYWRIGHT_CHROMIUM) return process.env.PLAYWRIGHT_CHROMIUM;
  if (!existsSync("/etc/NIXOS")) return undefined;
  const link = resolve(import.meta.dirname, "node_modules/.cache/nix-chromium");
  const bin = `${link}/bin/chromium`;
  if (!existsSync(bin)) {
    execFileSync(
      "nix",
      [
        "--extra-experimental-features",
        "nix-command flakes",
        "build",
        "nixpkgs#chromium",
        "--out-link",
        link,
      ],
      { stdio: "inherit" },
    );
  }
  return bin;
}

const executablePath = chromiumPath();

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  timeout: 60_000,
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${String(E2E_PORT)}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        ...(executablePath ? { launchOptions: { executablePath } } : {}),
      },
    },
  ],
});
