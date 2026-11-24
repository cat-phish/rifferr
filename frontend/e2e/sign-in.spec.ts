/**
 * F-SECURITY SECURITY-15 to -18: password sign-in, a session that survives a restart, and
 * signing out. Runs its own `rifferr serve` on its own port and temp folders, since it
 * restarts the server.
 */
import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { E2E_PORT } from "../playwright.config";

const backend = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "backend");
const uv = process.env.UV ?? "uv";
const PORT = E2E_PORT + 1;
const BASE = `http://127.0.0.1:${String(PORT)}`;
const PASSWORD = "a long e2e password";

let root = "";
let env: NodeJS.ProcessEnv = {};
let server: ChildProcess | null = null;

function rifferr(args: string[], input?: string): string {
  return execFileSync(uv, ["run", "--project", backend, "rifferr", ...args], {
    env,
    input,
    encoding: "utf8",
  });
}

async function start(): Promise<void> {
  const proc = spawn(
    uv,
    ["run", "--project", backend, "rifferr", "serve", "--no-browser", "--port", String(PORT)],
    { env, stdio: ["ignore", "inherit", "inherit"] },
  );
  server = proc;
  const serverFile = join(env.RIFFERR_DATA_DIR ?? "", "server.json");
  const deadline = Date.now() + 60_000;
  while (!existsSync(serverFile)) {
    if (proc.exitCode !== null)
      throw new Error(`rifferr serve exited with ${String(proc.exitCode)}`);
    if (Date.now() > deadline) throw new Error("rifferr serve did not start");
    await new Promise((r) => setTimeout(r, 100));
  }
}

async function stop(): Promise<void> {
  const proc = server;
  server = null;
  if (proc === null || proc.exitCode !== null) return;
  await new Promise<void>((done) => {
    proc.once("exit", () => {
      done();
    });
    proc.kill("SIGTERM");
  });
}

test.beforeAll(async () => {
  root = mkdtempSync(join(tmpdir(), "rifferr-e2e-auth-"));
  for (const d of ["config", "data", "run", "home"]) mkdirSync(join(root, d), { mode: 0o700 });
  env = {
    ...process.env,
    HOME: join(root, "home"),
    RIFFERR_CONFIG_DIR: join(root, "config"),
    RIFFERR_DATA_DIR: join(root, "data"),
    XDG_RUNTIME_DIR: join(root, "run"),
  };
  rifferr(["passwd", "--stdin"], `${PASSWORD}\n`);
  await start();
});

test.afterAll(async () => {
  await stop();
  rmSync(root, { recursive: true, force: true });
});

test("SECURITY_18 sign in with the password; the session survives a restart; sign out", async ({
  page,
}) => {
  await page.goto(`${BASE}/`);
  await expect(page.getByRole("heading", { name: "Signed out" })).toBeVisible();

  // A wrong password is refused with a plain message.
  await page.getByLabel("Password").fill("not the password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toHaveText("Wrong user name or password.");

  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("account-menu")).toBeVisible();

  // SECURITY-15: a restart keeps the session.
  await stop();
  await start();
  await page.reload();
  await expect(page.getByTestId("account-menu")).toBeVisible();

  // SECURITY-16: signing out ends it.
  await page.getByTestId("account-menu").click();
  await page.getByText("Sign out", { exact: true }).click();
  await expect(page.getByRole("heading", { name: "Signed out" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Signed out" })).toBeVisible();
});
