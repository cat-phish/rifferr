# S-BEETS-COMMAND: `beets.command` step
Status: Implemented
Milestone: M1

## Context
`beets.command` runs a `beet` subcommand on the albums a step selects, e.g.
`move` after a review gate, `replaygain -a`, `fetchart`, `mbsync`
(architecture §3). CLAUDE.md non-negotiable 2: beets is used in-process only where
the CLI can't do the job, so everything else runs the `beet` CLI as a subprocess,
exactly as in a terminal, with the profile's `BEETSDIR`. The selection reaches beets
as a query: the run marker (`rifferr_run`, BEETS-IMPORT-5) plus the step's `when`.

Unattended first (non-negotiable 6): the command must never wait for someone at a
terminal. A command that would prompt fails instead.

The step needs the backend to say how to start its CLI and how to write a selection
as CLI query arguments, because query syntax belongs to the backend (architecture §8).
That is a C-BACKEND amendment (below). The subprocess handling (streaming, redaction,
timeout, process groups) is the same as `shell` (S-SHELL SHELL-6, -7, -9, -13) and
shares its code.

Changes a `beet` subcommand makes (tags, paths, art) bypass ChangeSets, which C-JOURNAL
already lists as a non-goal to undo. Open question 2 asks how far the step should
record them, given non-negotiable 3.

## Behavior
- BEETS-COMMAND-1: The step MUST accept `command` (one subcommand name, matching `^[a-z][a-z0-9_-]*$`), `args` (a list of strings, default empty) and `timeout` (a duration as in S-REVIEW, default `1h`, open question 4). `command` or `args` given as a single string MUST be a validation error.
- BEETS-COMMAND-2: `command` and each `args` element MUST have `{{ }}` interpolated at step start (C-WORKFLOW). beets template syntax in `args` (`$path`, `%if{}`) MUST be passed to beets unchanged, not evaluated by Rifferr, because beets evaluates its own format options (e.g. `ls -f '$albumartist'`).
- BEETS-COMMAND-3: The subcommands `import` (use `beets.import`), `config` and `edit` (they open an editor) MUST be validation errors that name the reason (open question 3).
- BEETS-COMMAND-4: The selection MUST be the step's default selection narrowed by `when` (RUN-2), written as CLI query arguments by the backend (BACKEND-14). The run-marker term MUST always be one of them, so a `beet` command never runs on the whole library.
- BEETS-COMMAND-5: Before starting the subprocess, the step MUST query the selection through the backend. If no album matches, it MUST log "nothing to do" and finish successfully without running `beet`.
- BEETS-COMMAND-6: The argv MUST be the backend's CLI command (BACKEND-14), then `command`, then `args`, then the query arguments, with no shell and no word splitting. The query arguments MUST come last so they can't be read as option values.
- BEETS-COMMAND-7: The process MUST get the inherited environment plus `backend.command_env()` (`BEETSDIR`), `RIFFERR_RUN_ID` and `RIFFERR_PROFILE`; profile secrets MUST NOT be added, and inherited `env:` secret variables MUST be removed (as SHELL-4). stdin MUST be empty (`/dev/null`), so a prompt gets end-of-file. cwd MUST be the profile directory.
- BEETS-COMMAND-8: stdout and stderr MUST stream line by line into `run.step.log` events with secret redaction, as SHELL-6.
- BEETS-COMMAND-9: `timeout` and cancellation (RUN-8) MUST terminate the process group with SIGTERM, then SIGKILL after 5 seconds, as SHELL-7 and SHELL-9. A timeout counts as a failure.
- BEETS-COMMAND-10: A non-zero exit status, a signal or a timeout MUST fail the step with a message naming it (`exited with status N`, …). The step has one invocation and no per-target failures, so `on_error: continue` behaves like `skip` (RUN-5).
- BEETS-COMMAND-11: The worker MUST NOT hold a library handle while the subprocess runs (BEETS-IMPORT-14), so the two never compete for SQLite write locks.
- BEETS-COMMAND-12: After a successful command, the step MUST query the selection again and log, per album, the fields that changed compared with the snapshot from BEETS-COMMAND-5 (including `path` for moves), or "no field changes". Outputs: `albums` (selected) and `changed` (albums with a changed field). How these changes are recorded beyond the log is open question 2.
- BEETS-COMMAND-13: The step MUST require the run to be trusted, as SHELL-12, and the static safety report MUST list each `beets.command` step with its command and args (open question 1).
- BEETS-COMMAND-14: The step MUST require the backend capabilities `query` and `cli` (BACKEND-14).

### C-BACKEND amendment this spec needs
- BACKEND-14 (new): A backend MAY declare capability `cli` and implement `cli_command(subcommand: str, args: list[str], selection: Selection) -> list[str]`, the full argv for its command-line tool with the selection as query arguments last. `BeetsBackend` returns `[sys.executable, "-m", "beets", subcommand, *args, *query]`: the same Python and installed beets version and plugins as the worker (open question 6). The query is the run-marker term (`rifferr_run::^<id>$`) plus the `when` string split by beets' own rule (`shlex`, B-018). `FakeBackend` returns an argv to a test script that records its arguments.

## Interfaces
```python
class BeetsCommandParams(BaseModel):
    command: str                       # "move", "replaygain", ...
    args: list[str] = []
    timeout: Duration = "1h"
```
- Entry point `beets.command` → `rifferr.steps.beets_command:BeetsCommandStep`; `requires` query + cli.
- Events: `run.step.log` only (BEETS-COMMAND-8, -12).
- Outputs: `albums`, `changed` (BEETS-COMMAND-12).

As implemented (2026-11-12):
- The subprocess runner moved from `rifferr.steps.shell` to `rifferr.steps.process.run_process`, used by both steps; `shell` behaves as before.
- Failure messages are prefixed with the subcommand: `beet move exited with status 3`, `beet move timed out after 1h`.
- The change log ignores `mtime` (bookkeeping beets updates whenever it touches a file). Album fields copied to items show once, as the album field. An album no longer selected afterwards is logged as "no longer selected (removed from the library?)" and counts as changed.
- The safety report lists each step as a `command` finding (`beet move -a`) and `beet remove` also as `destructive`.
- `BeetsBackend` opened read-only and `FakeBackend.read_only_view()` have no `cli` capability. `FakeBackend.cli` is the argv prefix (default: a script that prints its arguments as JSON).

## Examples
```yaml
- id: check
  uses: review
  with: { message: 'Check $albumartist - $album before the files move' }
- id: move
  uses: beets.command
  with: { command: move }
- id: gain
  uses: beets.command
  when: 'format:FLAC'
  with: { command: replaygain, args: ['-a'], timeout: 2h }
```
For run `01ABC` that imported one FLAC album → the `move` step runs
`python -m beets move rifferr_run::^01ABC$` with `BEETSDIR` set, streams beets'
output to the run log, then logs `Artist - Album: path changed`. The `gain` step
runs `python -m beets replaygain -a rifferr_run::^01ABC$ format:FLAC`. If the run
imported nothing, both log "nothing to do" and `beet` never starts.

`with: { command: import }` → validation error "use the beets.import step to
import". `with: { command: 'move -a' }` → validation error (one subcommand name;
options go in `args`).

## Extensibility
- Other backends can offer their own CLI through BACKEND-14; the step stays the same.
- Users extend behavior through beets plugins, which the subprocess loads from the profile's beets config.

## Security & safety
- The subprocess gets an argv, never a shell string, so tag values and `when` text can't become shell code.
- The selection always carries the run marker (BEETS-COMMAND-4), so a mistyped `when` narrows the run's albums and never widens to the library.
- Commands can delete files (`remove -d`) or rewrite tags, so the step runs only in trusted workflows (BEETS-COMMAND-13).
- Secrets don't reach the process (BEETS-COMMAND-7).

## Non-goals
- One invocation per album (that arrives with lanes, M2).
- Parsing a command's output, or knowing what each subcommand does.
- Undoing file moves, fetched art or other file effects (C-JOURNAL non-goal).
- Interactive commands, or answering prompts.

## Open questions
Resolved 2026-11-12 (author: "go with your recommendations, approved"):
1. ~~Trust?~~ Yes, like `shell` (BEETS-COMMAND-13).
2. ~~ChangeSets vs non-negotiable 3?~~ (a) for M1: log the before/after diff only
   (BEETS-COMMAND-12). CLAUDE.md non-negotiable 3 names `beets.command` as the one
   exception. Recording the diff as an observed journal entry (option b) is B-039.
3. ~~Refused subcommands?~~ `import`, `config`, `edit` (BEETS-COMMAND-3). Prompting
   commands fail on empty stdin.
4. ~~Timeout?~~ A Duration, default `1h` (BEETS-COMMAND-1). Moving `shell` to
   Durations is B-040.
5. ~~Album vs item queries?~~ The same query works for both; `-a` stays in `args`.
6. ~~Which `beet`?~~ `python -m beets` from the worker's Python (BACKEND-14).

## Tasks
- [x] BACKEND-14: `cli` capability, `cli_command` on BeetsBackend and FakeBackend, contract test
- [x] params model + validation (refused subcommands, single-string errors)
- [x] share the process runner with `shell` (streaming, redaction, timeout, cancel)
- [x] selection query + nothing-to-do check
- [x] before/after diff log and outputs
- [x] trust check and safety report entry
- [x] beets-backend test: `move` on a temp profile moves the files (no real library)
