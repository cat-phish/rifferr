/**
 * Prepares a throwaway Rifferr install and starts `rifferr serve` on it.
 * Everything (config, data, runtime dir, beets config, library, inbox) lives in a temp
 * folder: no real music or beets config is touched (AGENTS.md testing rules).
 */
import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { E2E_PORT } from "../playwright.config";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "..", "..");
const backend = join(repo, "backend");
const uv = process.env.UV ?? "uv";

export default async function globalSetup(): Promise<() => Promise<void>> {
  const root = mkdtempSync(join(tmpdir(), "rifferr-e2e-"));
  const dirs = {
    config: join(root, "config"),
    data: join(root, "data"),
    run: join(root, "run"),
    beets: join(root, "beets"),
    library: join(root, "library"),
    inbox: join(root, "inbox"),
    home: join(root, "home"),
  };
  for (const d of Object.values(dirs)) mkdirSync(d, { recursive: true, mode: 0o700 });
  writeFileSync(
    join(dirs.beets, "config.yaml"),
    [
      `directory: ${dirs.library}`,
      `library: ${join(dirs.beets, "library.db")}`,
      "plugins: []",
      "threaded: no",
      "create_backup_before_migrations: no",
      "import:",
      "  autotag: no",
      "  copy: yes",
      "  write: no",
      "",
    ].join("\n"),
  );
  cpSync(
    join(backend, "tests", "fixtures", "audio", "album-flac"),
    join(dirs.inbox, "album-flac"),
    {
      recursive: true,
    },
  );
  const env = {
    ...process.env,
    HOME: dirs.home,
    RIFFERR_CONFIG_DIR: dirs.config,
    RIFFERR_DATA_DIR: dirs.data,
    XDG_RUNTIME_DIR: dirs.run,
    BEETSDIR: dirs.beets,
  };
  const rifferr = (...args: string[]): string =>
    execFileSync(uv, ["run", "--project", backend, "rifferr", ...args], { env, encoding: "utf8" });
  rifferr("profile", "create", "main", "--beetsdir", dirs.beets);
  // An album already in the library, for the tag-script preview (TAGSCRIPT-22). Its album
  // name differs from the inbox copy, so the two-step import isn't a duplicate.
  const library = join(root, "preimport");
  cpSync(join(backend, "tests", "fixtures", "audio", "album-flac"), join(library, "album-flac"), {
    recursive: true,
  });
  execFileSync(
    uv,
    [
      "run",
      "--project",
      backend,
      "beet",
      "import",
      "-q",
      "--set",
      "album=Preview Sessions",
      "--set",
      "albumtype=ep",
      join(library, "album-flac"),
    ],
    { env, encoding: "utf8" },
  );
  cpSync(
    join(repo, "examples", "workflows", "two-step.yaml"),
    join(dirs.config, "profiles", "main", "workflows", "two-step.yaml"),
  );

  const server: ChildProcess = spawn(
    uv,
    ["run", "--project", backend, "rifferr", "serve", "--no-browser", "--port", String(E2E_PORT)],
    { env, stdio: ["ignore", "inherit", "inherit"] },
  );
  const tokenFile = join(dirs.run, "rifferr", `${String(E2E_PORT)}.token`);
  const deadline = Date.now() + 60_000;
  while (!existsSync(join(dirs.data, "server.json"))) {
    if (server.exitCode !== null)
      throw new Error(`rifferr serve exited with ${String(server.exitCode)}`);
    if (Date.now() > deadline) throw new Error("rifferr serve did not start");
    await new Promise((r) => setTimeout(r, 100));
  }
  process.env.RIFFERR_E2E_TOKEN = readFileSync(tokenFile, "utf8").trim();
  process.env.RIFFERR_E2E_INBOX = dirs.inbox;

  return async () => {
    server.kill("SIGTERM");
    await new Promise<void>((done) => {
      if (server.exitCode !== null) done();
      else
        server.once("exit", () => {
          done();
        });
    });
    rmSync(root, { recursive: true, force: true });
  };
}
