# S-FILES-CLEANUP: `files.cleanup` step
Status: Draft
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
  `remove_root` (also remove `path` itself when it ends up empty, default `false`) and `trash`
  (optional folder: move instead of delete). `mode: delete` with neither `delete` patterns nor
  `empty_dirs` MUST be a validation error (nothing to do).
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

### Report and delete
- FILES-CLEANUP-8: The step MUST classify every file under `path` as `delete` (matched a
  `delete` pattern and no `keep` pattern), `keep` (matched `keep`), `in_library`, `busy`, or
  `left` (matched nothing), and every folder that would be empty afterwards as `empty`. It
  MUST log one line per file and folder with its class and size, and emit
  `run.step.cleanup.report` with the listing (paths relative to `path`, class, size; at most
  1000 entries, then a count of the rest) and totals per class.
- FILES-CLEANUP-9: In `report` mode the step MUST change nothing. In `delete` mode it MUST
  delete (or move to `trash`, keeping relative paths, never overwriting: a clash gets a
  numeric suffix) the `delete` files, then remove `empty` folders bottom-up, and `path` itself
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
  behind a new capability `files`. The step requires `files`.
- FILES-CLEANUP-13: `mode: delete` MUST run only in trusted workflows (F-SECURITY); in an
  untrusted one the step is refused (`StepRefused`), and `report` still runs. The static safety
  report MUST list every `files.cleanup` step in `delete` mode as `destructive`, with its
  `path`, patterns and `trash`.

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
```
- Event: `run.step.cleanup.report {entries, truncated, totals}`.
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
- An unmatched album's FLAC files: `left` (no pattern matches audio here); never deleted.

## Extensibility
- Patterns, `keep`, `trash` and the folder are workflow choices; there is no built-in list of
  "junk" extensions in engine code (non-negotiable 1). The example workflow carries one.
- Other backends implement `library_files` / `library_dirs` to get the same protection; a
  backend without the `files` capability can't run the step.

## Security & safety
- Default `report` mode, trusted-only `delete`, destructive finding in the safety report.
- Never outside `path`, never following symlinks, never library files, never busy folders,
  never `/`, home or library folders. Files changed since the listing are skipped.
- `trash` makes a run reversible by hand; there is no undo in the journal (non-goal).
- In Docker the step sees only mounted folders.

## Non-goals
- Undo of deletions through the journal, or deletions as ChangeSets.
- Moving leftovers elsewhere for manual tagging (checklist line 5): a `shell` or a later
  `files.move` step.
- Reviewing the deletion list in the review queue before deleting (open question 1).
- Built-in junk-file lists.

## Open questions
1. Should there be an `apply: review` style option, where `delete` mode stops with the listing
   in the review queue and deletes only after Continue? It needs a hold that isn't about an
   album (gates and holds are per lane today), so it would touch C-LANES. Drafted as a
   non-goal: report first, then switch the workflow to `delete`.
2. FILES-CLEANUP-12 adds two backend reads and a capability. Is that acceptable, or should the
   step only skip paths inside `library_dirs()` and not check individual files?
3. Should `delete` patterns match audio files at all, or must the step refuse patterns that
   would match files the backend can import (a second guard against deleting unmatched
   albums)? Drafted: allowed, because FILES-CLEANUP-6 and -7 already protect library files and
   pending decisions.
4. Should `trash` be emptied after some time (e.g. `trash_keep: 30d`), or is that the user's
   job?
5. Is "the input of another queued, running or waiting run" (FILES-CLEANUP-7) the right scope,
   or only pending import decisions?

## Tasks
- [ ] C-BACKEND amendment: `files` capability, `library_files`, `library_dirs`; FakeBackend and
      BeetsBackend; contract tests (FILES-CLEANUP-12)
- [ ] Walk and classify with symlink and outside-path guards (FILES-CLEANUP-3 to -8)
- [ ] Busy folders from import decisions and run inputs (FILES-CLEANUP-7)
- [ ] Report event, delete/trash, empty folders, outputs (FILES-CLEANUP-8 to -11)
- [ ] Trust refusal and safety-report finding (FILES-CLEANUP-13)
- [ ] Use it in `examples/workflows/foobar-style.yaml`; update `docs/coverage.md` row 4
