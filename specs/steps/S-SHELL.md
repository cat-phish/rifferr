# S-SHELL: `shell` step
Status: Draft
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
- SHELL-3: `per` MUST accept `run` (once), `album` (once per selected album) or `item` (once per selected item), default `album`.
- SHELL-4: The process environment MUST include `RIFFERR_RUN_ID`, `RIFFERR_PROFILE`, `RIFFERR_ALBUM_DIR` (for `per: album|item`), `RIFFERR_ITEM_PATH` (for `per: item`), every profile/workflow var as `RIFFERR_VAR_<NAME>` (upper-cased, non-alphanumerics → `_`), plus `backend.command_env()`. Profile secrets MUST NOT be added to the environment unless the step lists them in `with.secrets`.
- SHELL-5: stdin MUST receive the JSON snapshot of the current album (`per: album`), item (`per: item`) or `{"albums": [...]}` (`per: run`), matching the published snapshot schema (C-BACKEND).
- SHELL-6: stdout and stderr MUST stream line by line into `run.step.log` events tagged with stream name and target id, with secret redaction applied (PROFILE-5).
- SHELL-7: `timeout` (seconds, default 600) MUST terminate the process group with SIGTERM, then SIGKILL after 5 seconds, and count as a failure.
- SHELL-8: A non-zero exit code MUST count as a failure for that target and be handled by the step's `on_error` (RUN-5).
- SHELL-9: Cancellation (RUN-8) MUST terminate the process group the same way as a timeout.
- SHELL-10: `cwd` MAY be set (templated); it defaults to the album directory for `per: album|item` and the profile directory for `per: run`.
- SHELL-11: In M0, `emits` MUST accept only `none`; `changes` MUST be a validation error that says it arrives with ChangeSets (M1).
- SHELL-12: The step MUST refuse to run if the workflow is not trusted (F-SECURITY), emitting a clear `run.step.finished` failure, regardless of `on_error`.

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
- Should `per: album` run albums in parallel in M0, or strictly sequentially until lanes exist (M2)?
- Is 600 s a reasonable default timeout, or should there be no default?
- On Windows (not a target today), what replaces process groups? Assume Linux/macOS only?

## Tasks
- [ ] params model + validation
- [ ] argv templating without word splitting
- [ ] env + stdin snapshot
- [ ] streaming logs with redaction
- [ ] timeout + cancellation (process group)
- [ ] trust check hook
