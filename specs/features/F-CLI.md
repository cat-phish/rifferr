# F-CLI: `rifferr` command line (M0 slice)
Status: Draft
Milestone: M0

## Context
The `rifferr` command starts the server, runs workflows headless (for cron
and scripts), and validates workflow files (architecture §9 `cli.py`;
AGENTS.md commands). Unattended use is the main use (AGENTS.md
non-negotiable 6), so every command works without a terminal prompt.

## Behavior
- CLI-1: `rifferr serve [--profile P] [--host H] [--port N] [--no-auth] [--no-browser] [--dev]` MUST start the API server (F-API) with the F-SECURITY rules, print the tokenized URL, and open it in a browser unless `--no-browser`.
- CLI-2: `rifferr run <workflow> --profile P [--var k=v ...] [--follow/--no-follow]` MUST start a run. If a server is running for this config root it MUST submit through the API with the bearer token (SECURITY-8); otherwise it MUST run the workflow in-process through a worker with no server.
- CLI-3: With `--follow` (default when stdout is a TTY), `rifferr run` MUST stream events as human-readable lines; with `--json` it MUST print one event JSON per line.
- CLI-4: `rifferr run` MUST exit 0 when the run succeeds, 1 when it fails, 2 for validation errors, 130 when cancelled by Ctrl-C (which MUST cancel the run).
- CLI-5: `rifferr validate <files...> [--profile P]` MUST validate each file (C-WORKFLOW), print errors as `file:line:col: message`, print the static safety report (SECURITY-11) with `--safety`, and exit non-zero if any file is invalid. Without `--profile`, it MUST skip profile-dependent checks and say so.
- CLI-6: `rifferr trust <file> --profile P` MUST show the safety report, ask for confirmation unless `--yes`, and record the file's hash as trusted (SECURITY-10/12).
- CLI-7: `rifferr profile create <name> [--beetsdir DIR]` and `rifferr profile list` MUST manage profiles (PROFILE-8).
- CLI-8: No command MUST print a secret value, even with `--verbose`.
- CLI-9: Every command MUST support `--help`, and `rifferr --version` MUST print the Rifferr and beets versions.

## Interfaces
Built with the standard library `argparse` or a small CLI dependency (open question). Entry point: `[project.scripts] rifferr = "rifferr.cli:main"`.

## Examples
```bash
rifferr validate examples/workflows/*.yaml
rifferr run two-step --profile main --var inbox=/tmp/in
# 12:00:01 run 01J… started (two-step @ main)
# 12:00:02 [import] imported: A - B
# 12:00:05 [list-files] stdout: total 12
# 12:00:05 run 01J… succeeded
```

## Extensibility
Plugins cannot add subcommands in M0.

## Security & safety
- Bearer token is read from the 0600 token file, never accepted as a command-line argument (it would show in `ps`).
- `trust` always shows the safety report before granting trust.

## Non-goals
- Interactive TUI. Import decisions from the terminal (M2 goes through the UI/review queue).

## Open questions
- CLI library: stdlib `argparse`, or a dependency such as Typer (MIT) / Click (BSD)? Needs approval if a dependency.
- Should `rifferr run` without a server be allowed at all, or always require `serve` (simpler, one code path)?
- Workflow argument: name within the profile, a file path, or both?

## Tasks
- [ ] serve
- [ ] run (API path + serverless path), follow output, exit codes
- [ ] validate (+ --safety)
- [ ] trust
- [ ] profile create/list
- [ ] --version
