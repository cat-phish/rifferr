import { defineConfig, devices } from "@playwright/test";

/** UI-SHELL-13: end to end against a real `rifferr serve` with a temp profile. */
export const E2E_PORT = Number(process.env.RIFFERR_E2E_PORT ?? "8799");

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
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
