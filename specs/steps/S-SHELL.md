# S-SHELL: `shell` step
Status: Implemented
Milestone: M0

## Context
The `shell` step runs any command or script, once per run, album or item
(architecture §5). In M0 it is output-only (`emits: none`); `emits: changes`,
which turns stdout into a ChangeSet, lands with C-CHANGESET in M1. Shell steps
are the main reason the security model exists (F-SECURITY): they run only for
trusted workflows.

## Behavior
- SHELL-1: `with.run` MUST accept an argv list (preferred) or, only when `shell: true`, a single string run through `/bin/sh -c`. A string without `shell: true` MUST be a validation error.
- SHELL-2: Each argv element MUST have `{{ }}` interpolated at step start (C-WORKFLOW) and beets template syntax (`$path`, `%if{…}`) evaluated per target through `backend.evaluate`; templates MUST NOT be expanded into extra argv elements (no word splitting).
- SHELL-3: `per` MUST accept `run` (once), `album` (once per selected album) or `item` (once per selected item), default `album`. Targets run one at a time, in selection order (parallelism arrives with lanes, M2). With `per: run` there is no target, so beets templates are not evaluated.
- SHELL-4: The process environment MUST include `RIFFERR_RUN_ID`, `RIFFERR_PROFILE`, `RIFFERR_ALBUM_DIR` (for `per: album|item`), `RIFFERR_ITEM_PATH` (for `per: item`), every profile/workflow var as `RIFFERR_VAR_<NAME>` (upper-cased, non-alphanumerics → `_`), plus `backend.command_env()`. Profile secrets MUST NOT be added to the environment unless the step lists them in `with.secrets`; environment variables inherited from Rifferr's own environment that hold a profile `env:` secret MUST be removed unless listed.
- SHELL-5: stdin MUST receive the JSON snapshot of the current album (`per: album`), item (`per: item`) or `{"albums": [...]}` (`per: run`), matching the published snapshot schema (C-BACKEND).
- SHELL-6: stdout and stderr MUST stream line by line into `run.step.log` events tagged with stream name and target id, with secret redaction applied (PROFILE-5).
- SHELL-7: `timeout` (seconds, default 600) MUST terminate the process group with SIGTERM, then SIGKILL after 5 seconds, and count as a failure.
- SHELL-8: A non-zero exit code MUST count as a failure for that target and be handled by the step's `on_error` (RUN-5).
- SHELL-9: Cancellation (RUN-8) MUST terminate the process group the same way as a timeout.
- SHELL-10: `cwd` MAY be set (templated); it defaults to the album directory for `per: album|item` and the profile directory for `per: run`.
- SHELL-11: In M0, `emits` MUST accept only `none`; `changes` MUST be a validation error that says it arrives with ChangeSets (M1).
- SHELL-12: The step MUST refuse to run if the run is not trusted (the engine's per-run `trusted` flag, default false; F-SECURITY decides it from the workflow's content hash). The refusal fails the step and stops the run regardless of `on_error`.
- SHELL-13: Shell steps MUST support Linux and macOS (POSIX process groups). Windows is not supported natively; use the Docker image.

## Interfaces
```python
class ShellParams(BaseModel):
    run: list[str] | str
    shell: bool = False
    per: Literal["run", "album", "item"] = "album"
    timeout: PositiveInt = 600
    cwd: str | None = None
    env: dict[str, str] = {}             # extra vars, templated
    secrets: list[str] = []              # profile secret names to expose as env
    emits: Literal["none"] = "none"      # "changes" added in M1
```

As implemented (`rifferr.steps.shell`, entry point `shell`):
- `requires` query + templates; `backend.command_env()` is added when the backend offers it.
- `shell: true` with a list, an empty `run`, and invalid `env` names are validation errors.
- A secret listed in `with.secrets` is exposed as `RIFFERR_SECRET_<NAME>` (upper-cased,
  non-alphanumerics → `_`); an inherited `env:` secret variable stays only if its secret is listed.
- Env precedence: inherited < `command_env()` < `RIFFERR_VAR_*` < `with.env` < secrets <
  `RIFFERR_RUN_ID` / `RIFFERR_PROFILE` / `RIFFERR_ALBUM_DIR` / `RIFFERR_ITEM_PATH`.
- Failure messages: `exited with status N`, `killed by signal SIGX`, `timed out after N s`,
  `could not start <prog>: …`, `working directory … does not exist`, `template error: …`.
  With `per: run` these fail the step; otherwise they fail the target (RUN-5).
- Output lines longer than 64 KiB are split into several events; output pipes held open by a
  background child are not waited for after the process exits (1 s).
- The step stops starting new targets once the run is cancelled.

## Examples
```yaml
- id: list-files
  uses: shell
  with:
    run: ["ls", "-la", "$path"]
    per: album
    timeout: 30
```
For album at `/music/A/B` → argv `["ls", "-la", "/music/A/B"]`, cwd `/music/A/B`,
stdin = album snapshot JSON, `RIFFERR_ALBUM_DIR=/music/A/B`.

`run: ["echo", "$title"]` where title is `a b; rm -rf /` → one argv element `a b; rm -rf /`, printed literally.

## Extensibility
None beyond parameters; users extend behavior with their own scripts.

## Security & safety
- argv lists avoid shell injection from tag values (SHELL-2). `shell: true` is flagged in the static safety report (F-SECURITY).
- Secrets only reach the process when named in `with.secrets`, and are redacted from its output.
- Runs inside the container in Docker mode; can only see mounted paths.

## Non-goals
- `emits: changes` (M1, after C-CHANGESET).
- Resource limits beyond timeout (memory, CPU).

## Open questions
- ~~Parallel `per: album` in M0?~~ Resolved 2026-11-11 (author): sequential until lanes (SHELL-3).
- ~~Default timeout?~~ Resolved 2026-11-11 (author): 600 s.
- ~~Windows?~~ Resolved 2026-11-11 (author): Linux and macOS only (SHELL-13).
- Trust before F-SECURITY: approved 2026-11-11 (author): per-run `trusted` flag, default false (SHELL-12).
- ~~Beets templates in a `shell: true` string?~~ Resolved 2026-11-11 (author): not evaluated,
  because tag values would become shell code; scripts read `RIFFERR_ALBUM_DIR` /
  `RIFFERR_ITEM_PATH` or stdin, or use `[sh, -c, '…"$1"…', sh, $path]`.

## Tasks
- [x] params model + validation
- [x] argv templating without word splitting
- [x] env + stdin snapshot
- [x] streaming logs with redaction
- [x] timeout + cancellation (process group)
- [x] trust check hook
