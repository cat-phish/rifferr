# S-SHELL: `shell` step
Status: Draft
Milestone: M1

## Context
The `shell` step runs any command or script, once per run, album or item
(architecture §5). In M0 it is output-only (`emits: none`); `emits: changes`,
which turns stdout into a ChangeSet, lands with C-CHANGESET in M1. Shell steps
are the main reason the security model exists (F-SECURITY): they run only for
trusted workflows.

**M1 update (Draft, 2026-11-11).** SHELL-1 to -10 and -12, -13 are implemented and
unchanged. SHELL-11 is replaced and SHELL-14 to -19 are new: with `emits: changes`,
the script's stdout is a ChangeSetProposal (C-CHANGESET CHANGESET-3). Author
decisions: in `per: album` / `per: item` a script may omit the target and mean the
current one; proposals are split into one ChangeSet per album. Both need the
C-CHANGESET amendments listed below.

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
- SHELL-11: `emits` MUST accept `none` (default) or `changes`. (Replaces the M0 rule that rejected `changes`.)
- SHELL-12: The step MUST refuse to run if the run is not trusted (the engine's per-run `trusted` flag, default false; F-SECURITY decides it from the workflow's content hash). The refusal fails the step and stops the run regardless of `on_error`.
- SHELL-13: Shell steps MUST support Linux and macOS (POSIX process groups). Windows is not supported natively; use the Docker image.
- SHELL-14: With `emits: changes`, stdout MUST NOT be streamed as log lines. It is collected (at most 16 MiB; more fails the target) and, after exit status 0, parsed as one JSON ChangeSetProposal. Empty or whitespace-only stdout means no changes. stderr streams as usual (SHELL-6). With a non-zero exit, timeout or cancellation, stdout is discarded and nothing is proposed.
- SHELL-15: With `per: album` or `per: item`, the proposal MAY use the short form `{"rationale"?, "changes": [FieldChange…]}`, which targets the current album or item. `per: run` MUST use `targets`. A proposal with both `changes` and `targets` is invalid.
- SHELL-16: Every target MUST lie within the invocation's scope: with `per: album`, the current album and its items; with `per: item`, the current item; with `per: run`, the selected albums and their items. A target outside it fails the target, and nothing from that invocation is proposed.
- SHELL-17: Output that isn't valid JSON, doesn't match the proposal schema, or has problems reported by `backend.check` (CHANGESET-2) MUST fail the target (handled by `on_error`, RUN-5), with a message naming the first problem and its JSON path, and nothing from that invocation is proposed.
- SHELL-18: A valid proposal MUST go through `ctx.propose` (source `step` / `shell`). `old` values the script omits are filled from the snapshot it got on stdin (SHELL-5), so a value changed by someone else in between shows up as a conflict at apply time. The step's `apply` policy then applies (CHANGESET-6, -7).
- SHELL-19: For each target, the step MUST log one summary line: the number of ChangeSets and field changes proposed, or "no changes".

### C-CHANGESET amendments this update needs
- CHANGESET-3 (amend): `ChangeSetProposal` MUST accept either `targets` or the short form `changes`. `ctx.propose(proposal, current=TargetRef | None)` resolves the short form to `current`, and rejects it without one. The published schema allows both forms, exclusively.
- CHANGESET-14 (new): `ctx.propose` MUST split a proposal into one ChangeSet per album. Each holds the album's own target (if any) and the targets of its items, grouped by the snapshot's album membership, in the order they appear. Each ChangeSet keeps the proposal's `rationale`. `propose` returns the ids of the ChangeSets it created (empty when everything was a no-op, CHANGESET-4).

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
    emits: Literal["none", "changes"] = "none"
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

A `per: album` script that sets `albumtype` from the track count it reads on stdin:
```yaml
- id: ep-type
  uses: shell
  apply: review
  with:
    run: [python3, scripts/ep_type.py]
    emits: changes
```
```python
import json, sys
album = json.load(sys.stdin)
if len(album["items"]) <= 6 and album["fields"].get("albumtype") != "ep":
    print(json.dumps({"rationale": "6 tracks or fewer",
                      "changes": [{"field": "albumtype", "new": "ep"}]}))
```
For `album-7` with 5 tracks and `albumtype: album` → one ChangeSet
`albumtype: album → ep` (old filled from the snapshot), logged as
`album-7: proposed 1 ChangeSet, 1 field change`; run → `waiting_review`.
On a re-run after it was applied, the script prints nothing → `no changes`.

`run: ["echo", "$title"]` where title is `a b; rm -rf /` → one argv element `a b; rm -rf /`, printed literally.

## Extensibility
None beyond parameters; users extend behavior with their own scripts.

## Security & safety
- argv lists avoid shell injection from tag values (SHELL-2). `shell: true` is flagged in the static safety report (F-SECURITY).
- Secrets only reach the process when named in `with.secrets`, and are redacted from its output.
- Runs inside the container in Docker mode; can only see mounted paths.
- With `emits: changes` a script can only propose. Its ChangeSets go through the engine and journal like any other, and can't reach albums outside its scope (SHELL-16).

## Non-goals
- Proposing file moves or renames (C-CHANGESET non-goal).
- Several proposals per invocation (JSON Lines); one JSON document per process.
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
- [ ] M1: `emits: changes` (SHELL-11, -14 to -19) + CHANGESET-3 short form, CHANGESET-14 split
- [x] params model + validation
- [x] argv templating without word splitting
- [x] env + stdin snapshot
- [x] streaming logs with redaction
- [x] timeout + cancellation (process group)
- [x] trust check hook
