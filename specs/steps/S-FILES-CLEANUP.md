# S-FILES-CLEANUP: `files.cleanup` step
Status: Implemented
Milestone: M2

## Context
After beets imports an inbox folder, files it didn't take are left behind: `.nfo`, `.m3u`,
`.sfv`, cover scans, rip logs, sometimes whole albums it couldn't match. The reference
checklist does this by hand (docs/original-checklist.md line 4, coverage row 4): "Check for
leftover misc. files and delete empty folders in /music/New/Stage-1-Unsorted", then "Check
… for any leftovers and manually edit tags". Architecture §3 lists the step as `files.cleanup`:
"Report/delete leftover files and empty dirs in a folder. Dry-run listing first."

Deleting files is the one thing in Rifferr that can't be undone from the journal (C-CHANGESET
non-goal: no file operations in ChangeSets). The step is therefore built around a report: by
default it only lists what is left. It deletes only what a workflow's patterns name, never
leaves its folder, never touches files the library knows, and never touches a folder an import
decision still waits on (unattended first: a pending album must survive the night).

## Behavior
### Parameters
- FILES-CLEANUP-1: The step MUST accept `path` (a folder; `{{ }}` interpolated, e.g.
  `{{ run.input }}`), `mode` (`report` or `delete`, default `report`), `delete` (glob patterns
  of files to delete, matched case-insensitively against the path relative to `path`, e.g.
  `"*.nfo"`, `"**/*.sfv"`; default none), `keep` (glob patterns never deleted, checked first;
  default none), `empty_dirs` (remove folders that are empty afterwards, default `true`),
  `remove_root` (also remove `path` itself when it ends up empty, default `false`), `trash`
  (optional folder: move instead of delete), and `review` (`false` | `true`, default `false`)
  with optional `timeout` (a Duration) and `on_timeout` (`keep` | `delete`, default `keep`),
  and `allow_audio` (default `false`, FILES-CLEANUP-17).
  `mode: delete` with neither `delete` patterns nor `empty_dirs` MUST be a validation error
  (nothing to do); so MUST `review` in `report` mode and `on_timeout` without `timeout`.
- FILES-CLEANUP-2: The step MUST be `run_level` (LANES-6): it runs once per pass, about a
  folder, not about albums. Its `when:` is not used and MUST be a validation error.

### Walking the folder
- FILES-CLEANUP-3: `path` MUST be absolute after interpolation, exist and be a directory, or
  the step fails (no target). It MUST NOT be `/`, the user's home directory, or a folder that
  is or contains one of the backend's library directories (FILES-CLEANUP-12).
- FILES-CLEANUP-4: The step MUST walk `path` without following symbolic links. A symlink is
  listed as a leftover and may be deleted (the link, never its target) when a pattern matches
  it. Nothing outside `path` may be read or changed; a path that resolves outside it (`..`,
  a symlinked folder) MUST be skipped and logged.
- FILES-CLEANUP-5: Hidden files and folders MUST be included, as the checklist's
  `Get-ChildItem -Force` does.

### What it never touches
- FILES-CLEANUP-6: A file the library knows (an item's path in the backend) MUST NOT be
  deleted or moved, whatever the patterns say. It is listed as `in_library`.
- FILES-CLEANUP-7: A folder that is, contains or lies inside the folder of a pending import
  decision of the same profile (F-IMPORT-UI), or the input of another queued, running or
  waiting run (F-TRIGGERS dedup), MUST be skipped as a whole and listed as `busy`, with the
  reason. The rest of `path` is cleaned normally.

- FILES-CLEANUP-17: A file the backend could import (beets: a format it reads) MUST NOT be
  deleted or moved unless the step sets `allow_audio: true`, even when a `delete` pattern
  matches it. It is listed as `left` with reason `importable`, so unmatched albums beets
  skipped without asking survive. With `allow_audio: true` the safety report's `destructive`
  finding says so.

### Report and delete
- FILES-CLEANUP-8: The step MUST classify every file under `path` as `delete` (matched a
  `delete` pattern and no `keep` pattern), `keep` (matched `keep`), `in_library`, `busy`, or
  `left` (matched nothing), and every folder that would be empty afterwards as `empty`. It
  MUST log one line per file and folder with its class and size, and emit
  `run.step.cleanup.report` with the listing (paths relative to `path`, class, size; at most
  1000 entries, then a count of the rest) and totals per class.
- FILES-CLEANUP-9: In `report` mode the step MUST change nothing. In `delete` mode it MUST
  delete (or move to `trash`, FILES-CLEANUP-15) the `delete` files, then remove `empty` folders bottom-up, and `path` itself
  only with `remove_root: true`. A file that changed between listing and deleting (size or
  mtime) MUST be skipped and logged.
- FILES-CLEANUP-10: A failure to delete one file MUST be logged and counted, and the step goes
  on; the step fails (no target, RUN-5) only if any deletion failed. Outputs: `deleted`,
  `moved`, `dirs_removed`, `left`, `in_library`, `busy`, `failed` (counts) and `bytes_freed`.
- FILES-CLEANUP-11: `left` files are never deleted. When any are found, the step MUST log them
  as a warning; a workflow can follow with a `notify` step on `{{ steps.<id>.left }}` (S-NOTIFY)
  so leftovers that need a human (unmatched albums) aren't missed.

### Backend and trust
- FILES-CLEANUP-12 (C-BACKEND amendment): `LibraryBackend` MUST gain a read
  `library_files(paths: list[Path]) -> set[Path]` (which of these files are items) and
  `library_dirs() -> list[Path]` (folders the library keeps music in; beets: `directory`),
  and `importable(paths: list[Path]) -> set[Path]` (which files it could import), behind a new
  capability `files`. The step requires `files`.
- FILES-CLEANUP-13: `mode: delete` MUST run only in trusted workflows (F-SECURITY); in an
  untrusted one the step is refused (`StepRefused`), and `report` still runs. The static safety
  report MUST list every `files.cleanup` step in `delete` mode as `destructive`, with its
  `path`, patterns and `trash`.

### Review before deleting
- FILES-CLEANUP-14: With `review: true`, the step MUST list as in FILES-CLEANUP-8, delete
  nothing, and leave a *cleanup decision* in the review queue holding the listing, `path` and
  the step's options, then hold the pass (C-LANES amendment below). The decision is `delete`
  or `keep` (nothing deleted), with an optional note, decided once (`409 already_resolved`
  after that). `delete` runs as a worker job that re-lists `path` and deletes only entries that
  were in the reviewed listing as `delete` or `empty` and haven't changed since (size, mtime);
  anything new or changed is left and logged. Then the held lanes resume at the next step, as
  they do after `keep`. With `timeout`, the server decides `on_timeout` once it passes
  (decided by `timeout`), also after a restart, as REVIEW-9. Cancelling the run cancels the
  decision. Events: `cleanup.requested`, `cleanup.resolved`. For S-NOTIFY the decision is a
  queue item like the others: hold kind `cleanup` counts for `review.needed` (NOTIFY-8) and
  reminders (NOTIFY-15).
- C-LANES amendment (LANES-16, new): A `run_level` step MAY hold its whole pass with
  `ctx.hold_pass(kind, ref)`: every lane of that pass becomes `waiting` on the hold. If the pass
  had no lanes, the run itself holds it: the run is `waiting_review` until it is resolved, then
  resumes at the next step (RUN-14).
- F-REVIEW-QUEUE amendment: `QueueItem.kind` gains `cleanup` (`cleanup_decision` field); the
  queue shows the listing grouped by class with Delete and Keep everything;
  `GET /api/cleanups/{id}`, `POST /api/cleanups/{id}/decision {decision: delete|keep, note?}`.

### Trash
- FILES-CLEANUP-15: With `trash`, files MUST be moved to `<trash>/<run_id>/<path relative to
  path>`, never overwriting (a clash gets a numeric suffix). The step MUST create
  `<trash>/.rifferr-trash` on first use and refuse a `trash` folder that exists, isn't empty
  and has no such marker, so it never adopts a folder it didn't create. `trash` MUST NOT be
  inside `path`.
- FILES-CLEANUP-16: The profile setting `files.trash_keep` (a Duration, default none: keep
  forever) MUST make every `files.cleanup` step, when it starts, permanently delete run
  folders in its marked `trash` whose newest file is older than `trash_keep`. Only direct
  children named like run ids are considered; anything else in the trash is left alone and
  logged once.

## Interfaces
```python
class CleanupParams(BaseModel):
    path: str
    mode: Literal["report", "delete"] = "report"
    delete: list[str] = []
    keep: list[str] = []
    empty_dirs: bool = True
    remove_root: bool = False
    trash: str | None = None
    review: bool = False
    allow_audio: bool = False
    timeout: Duration | None = None
    on_timeout: Literal["keep", "delete"] | None = None   # default keep when timeout set

# profile.yaml
class FilesSettings(BaseModel):
    trash_keep: Duration | None = None

CleanupClass = Literal["delete", "keep", "in_library", "busy", "left", "empty"]

class CleanupEntry(BaseModel, frozen=True):
    path: str                    # relative to `path`
    kind: Literal["file", "dir", "link"]
    cls: CleanupClass
    size: int | None
    reason: str | None           # busy: which decision or run

# LibraryBackend (C-BACKEND)
def library_files(self, paths: list[Path]) -> set[Path]: ...
def library_dirs(self) -> list[Path]: ...
def importable(self, paths: list[Path]) -> set[Path]: ...
```
- Event: `run.step.cleanup.report {entries, truncated, totals}`; `cleanup.requested
  {decision_id, path, totals}`, `cleanup.resolved {decision_id, decision, decided_by}`.
- App DB: `cleanup_decisions` (id, profile, run_id, step_id, state, body JSON, created_at,
  deadline, decided_at, decided_by, note). Worker job kind `cleanup_apply`.
- Safety report finding: `destructive` (`files.cleanup` delete mode).

## Examples
```yaml
# after the import, per inbox folder (watch trigger: run.input is one top-level folder)
- id: cleanup
  uses: files.cleanup
  with:
    path: "{{ run.input }}"
    mode: delete
    delete: ["*.nfo", "*.m3u", "*.m3u8", "*.sfv", "*.url", "Thumbs.db", ".DS_Store"]
    keep: ["*.log", "*.cue"]        # rip logs may matter later (B-045)
    remove_root: true
- id: leftovers                    # sends every time: steps can't be skipped on outputs yet
  uses: notify
  with:
    title: "Leftovers in {{ run.input }}"
    body: "{{ steps.cleanup.left }} files beets didn't import"
```
- Inbox folder imported fully, leaving `album.nfo` and an empty `Scans/`: `album.nfo` deleted,
  `Scans/` and the folder removed; outputs `deleted: 1, dirs_removed: 2, left: 0`.
- Same folder, but the import left it as an import decision: the whole folder is `busy`,
  nothing changes, and the log names the decision.
- An unmatched album's FLAC files with `delete: ["**/*"]`: `left`, reason `importable`; never
  deleted unless `allow_audio: true`.

## Extensibility
- Patterns, `keep`, `trash` and the folder are workflow choices; there is no built-in list of
  "junk" extensions in engine code (non-negotiable 1). The example workflow carries one.
- Other backends implement `library_files` / `library_dirs` to get the same protection; a
  backend without the `files` capability can't run the step.

## Security & safety
- Default `report` mode, trusted-only `delete`, destructive finding in the safety report.
- Never outside `path`, never following symlinks, never library files, never busy folders,
  never `/`, home or library folders. Files changed since the listing are skipped.
- `trash` makes a run reversible by hand until `trash_keep` passes; there is no undo in the
  journal (non-goal). The trash marker stops the step from emptying a folder it didn't make.
- `review: true` deletes only what a person saw in the listing, unchanged since.
- In Docker the step sees only mounted folders.

Implementation notes (2026-11-14, added while implementing; not new behavior):
- Globs: a pattern without `/` matches a file name at any depth, one with `/` the path relative to `path`; `**` spans folders; matching ignores case.
- After a reviewed `delete`, the held lanes go on even if some files couldn't be removed; failures are in the run log and the decision's `result`. The step's own outputs are from the listing (`to_delete`, `left`, …) plus `decision`.
- A cleanup decision can be answered only while its run is `waiting_review` (`409 not_waiting`, as gates).
- Busy run inputs are taken from every profile's queued, running and waiting runs; import decisions from the step's own profile.

## Non-goals
- Undo of deletions through the journal, or deletions as ChangeSets.
- Moving leftovers elsewhere for manual tagging (checklist line 5): a `shell` or a later
  `files.move` step.
- Built-in junk-file lists.

## Open questions
Resolved (author, 2026-11-14):
1. Review before deleting: yes (FILES-CLEANUP-14, with a C-LANES and an F-REVIEW-QUEUE
   amendment).
2. The backend reads and the `files` capability are fine (FILES-CLEANUP-12).
3. Importable audio is never deleted unless `allow_audio: true` (FILES-CLEANUP-17).
4. Emptying the trash is a profile setting, `files.trash_keep` (FILES-CLEANUP-16).
5. Busy covers both pending import decisions and other active runs' inputs
   (FILES-CLEANUP-7): a nightly sweep must not clean a folder another run is importing.

## Tasks
- [x] C-BACKEND amendment: `files` capability, `library_files`, `library_dirs`, `importable`;
      FakeBackend and
      BeetsBackend; contract tests (FILES-CLEANUP-12)
- [x] Walk and classify with symlink and outside-path guards (FILES-CLEANUP-3 to -8)
- [x] Busy folders from import decisions and run inputs (FILES-CLEANUP-7)
- [x] Report event, delete/trash, empty folders, outputs (FILES-CLEANUP-8 to -11)
- [x] Trust refusal and safety-report finding (FILES-CLEANUP-13)
- [x] `ctx.hold_pass` and run-level holds (C-LANES LANES-16)
- [x] Cleanup decisions: store, worker job, timeout, cancel, events, S-NOTIFY hold kind
      (FILES-CLEANUP-14)
- [x] Review queue: `cleanup` items, routes and UI, with a Playwright test (FILES-CLEANUP-14)
- [x] Trash layout, marker and `files.trash_keep` purge (FILES-CLEANUP-15, -16)
- [x] Use it in `examples/workflows/foobar-style.yaml`; update `docs/coverage.md` row 4
