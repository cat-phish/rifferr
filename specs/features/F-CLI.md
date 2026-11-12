# F-CLI: `rifferr` command line (M0 slice)
Status: Draft
Milestone: M0

## Context
The `rifferr` command starts the server, runs workflows headless (for cron
and scripts), and validates and trusts workflow files (architecture §9
`cli.py`; AGENTS.md commands). Unattended use is the main use (AGENTS.md
non-negotiable 6), so no command ever waits on a terminal prompt unless a
terminal is attached.

In M0, `rifferr run` always goes through a running server (F-API). Serverless
runs, which would need a system-wide worker lock per profile, are B-023.

## Behavior
- CLI-1: `rifferr serve [--host H] [--port N] [--no-auth] [--no-browser] [--dev] [--vite-port N]` MUST start the F-API app (`create_app`) under uvicorn. It MUST call `check_bind` before listening (SECURITY-1, -9), so a non-loopback `--host` refuses to start in M0 with exit 2. Defaults: host `127.0.0.1`, port 8765, Vite port 5173. A port already in use MUST exit 1 with a message naming the port.
- CLI-2: At startup `serve` MUST write a server file `<data_root>/server.json` (mode 0600) with `{port, pid, no_auth, started_at}`, holding no token, and remove it on shutdown. A server file whose `pid` is not alive is stale and is overwritten. This is how other commands find the server for this config root.
- CLI-3: `serve` MUST open `http://127.0.0.1:<port>/auth/exchange?token=…` in a browser unless `--no-browser`. It MUST print that tokenized URL only when stdout is a TTY. Otherwise (service, Docker, redirected output) it prints the URL without the token and the token file's path, so the token never reaches a log (SECURITY-8).
- CLI-4: `serve` MUST never log the token. uvicorn's access log MUST redact the query string of `/auth/exchange` (or the access log is off), and no Rifferr log line contains it.
- CLI-5: `serve --dev` MUST pass `dev=True` to F-API (Vite origin, API-13) and start `pnpm dev --port <vite port> --strictPort` in the repo's `frontend/` folder as a child process, with `RIFFERR_BACKEND=http://127.0.0.1:<port>`. The browser opens the Vite URL's `/auth/exchange`, which Vite proxies to the backend. Without a `frontend/` folder or `pnpm`, `--dev` MUST exit 2 with a message. The Vite process MUST be stopped when `serve` exits.
- CLI-6: `serve` MUST shut down cleanly on SIGINT and SIGTERM: stop accepting requests, stop the workers (WORKER-8), stop Vite, and remove the token and server files.
- CLI-7: `rifferr run <workflow> --profile P [--var k=v ...] [--follow | --no-follow] [--json]` MUST submit a run through the API (`POST /api/runs`) using the server file and the bearer token from the token file. `<workflow>` is a file stem in the profile's `workflows/` folder (API-3). With no running server it MUST exit 3 and say to start `rifferr serve`.
- CLI-8: With `--follow` (the default when stdout is a TTY), `run` MUST stream the run's events (`/api/runs/{id}/events`) as human-readable lines: time, step id, message. With `--json` it MUST print one event JSON per line instead. With `--no-follow` it MUST print the run id and exit 0 once the run is queued.
- CLI-9: When following, `run` MUST exit 0 if the run succeeds and 1 if it fails or is cancelled from elsewhere. It MUST exit 2 for usage or validation errors (`422 validation_failed`, printed as `file:line:col: message`) and 3 when the server can't be reached. On the first Ctrl-C or SIGTERM it MUST request cancellation (`POST /api/runs/{id}/cancel`), wait for `run.finished` and exit 130 (SIGINT) or 143 (SIGTERM). A second signal MUST exit at once without waiting.
- CLI-10: If a run fails because a step needed trust (`StepRefused`), `run` MUST print a hint naming `rifferr trust <file>`.
- CLI-11: `rifferr validate <files...> [--profile P] [--safety]` MUST validate each file path (C-WORKFLOW) without backend capabilities (like the server, it never loads beets). It MUST print errors as `file:line:col: message`, and with `--safety` print the static safety report (SECURITY-11). Without `--profile` it MUST skip the profile-dependent checks and say so once. It MUST exit 0 if every file is valid and 1 otherwise; safety findings don't change the exit code.
- CLI-12: `rifferr trust <file> [--profile P] [--yes]` MUST read the file's bytes once, show the safety report for those bytes, and ask for confirmation unless `--yes`. Only then does it record their hash as trusted (`trusted_by: "cli"`, SECURITY-10, -12). It writes the app DB directly, so it works with or without a running server. Without a TTY and without `--yes` it MUST exit 2 without prompting. With `--profile`, it also validates against that profile first and refuses (exit 1) if the file is invalid.
- CLI-13: `rifferr profile create <name> [--beetsdir DIR]` MUST create a profile (PROFILE-8) and print its path; an existing name exits 1. `rifferr profile list` MUST print each profile's name and backend, or its load error.
- CLI-14: No command MUST print a secret value or a secret reference's target, at any verbosity (`-v` / `--verbose` raises the log level only).
- CLI-15: Every command MUST support `--help`. `rifferr --version` MUST print the Rifferr and beets versions; the beets version comes from `importlib.metadata`, not from importing beets.

## Interfaces
- Standard library `argparse` with subparsers (author, 2026-11-11). Entry point: `[project.scripts] rifferr = "rifferr.cli:main"`; `main(argv: list[str] | None = None) -> int`.
- `rifferr.cli` package: `main.py` (parser, dispatch), `serve.py`, `run.py`, `files.py` (validate, trust), `profiles.py`, `client.py` (server discovery from `server.json`, token from the token file, a small httpx client with `Authorization: Bearer` and `Host: 127.0.0.1:<port>`).
- The CLI client needs an HTTP client with streaming; `httpx` is installed today only for tests. **Dependency question** (see Open questions).
- Server file (`<data_root>/server.json`):
  ```json
  {"port": 8765, "pid": 4242, "no_auth": false, "started_at": "2026-11-11T12:00:00Z"}
  ```
- Exit codes: 0 success, 1 run failed / invalid file / runtime error, 2 usage or validation error or refused bind, 3 server not reachable, 130 / 143 cancelled by SIGINT / SIGTERM.

## Examples
```bash
rifferr validate examples/workflows/*.yaml --safety
rifferr trust ~/.config/rifferr/profiles/main/workflows/two-step.yaml
rifferr run two-step --profile main --var inbox=/tmp/in
# 12:00:01 run 01J… started (two-step @ main)
# 12:00:02 [import] imported: A - B
# 12:00:05 [list-files] stdout: total 12
# 12:00:05 run 01J… succeeded
rifferr run two-step --profile main --no-follow     # cron: prints the run id
rifferr serve --no-browser                           # as a service: no token in the log
```

## Extensibility
Plugins cannot add subcommands in M0.

## Security & safety
- The bearer token is read from the 0600 token file. It is never accepted as a command-line argument, which would show in `ps`.
- The token appears on screen only when stdout is a TTY, and never in logs, including uvicorn's access log (CLI-3, -4).
- `trust` always shows the safety report for exactly the bytes it hashes, and never prompts without a TTY.
- `serve --host` beyond loopback refuses to start in M0 (SECURITY-9), so the Docker image needs real auth (M1) before it can listen on `0.0.0.0`.

## Non-goals
- Interactive TUI. Import decisions from the terminal (M2 goes through the UI and review queue).
- Running a workflow without a server (B-023).
- `rifferr run <path>`: runs only take workflows inside a profile.

## Open questions
- ~~CLI library?~~ Resolved 2026-11-11 (author): stdlib `argparse`.
- ~~Serverless `run`?~~ Resolved 2026-11-11 (author): M0 requires `serve`; serverless → B-023.
- ~~Workflow argument?~~ Resolved 2026-11-11 (author): `run` takes a file stem in the profile; `validate` and `trust` take file paths.
- ~~`serve --dev`?~~ Resolved 2026-11-11 (author): also spawns Vite (`pnpm dev`) and opens its URL.
- `httpx` as a runtime dependency (BSD-3-Clause; today it is installed only through the dev group, via respx)? The alternative is `urllib.request` plus a hand-written SSE line reader.
- AGENTS.md lists `uv run rifferr serve --profile dev --dev`. `serve` has no `--profile` (the server serves every profile), so that line becomes `uv run rifferr serve --dev`. Is that OK?

## Tasks
- [ ] `[project.scripts]` entry, parser, `--version`, `-v`
- [ ] serve: bind check, server file, token URL on TTY only, access-log redaction, signals
- [ ] serve --dev: Vite child process
- [ ] run: discovery, submit, follow (text / JSON), exit codes, Ctrl-C cancel, trust hint
- [ ] validate (+ --safety)
- [ ] trust
- [ ] profile create / list
- [ ] secret-leak test over every command's output
