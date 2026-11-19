# S-LIBRARY-MOVE: `library.move` step (journaled rename, move and copy)
Status: Implemented
Milestone: M5

## Context
The reference checklist ends by renaming files with foobar2000 file operations, renaming the
Collaboration/Splits/Compilations/Soundtracks folders and copying the result into the library
(docs/original-checklist.md "Run file operations…", "Rename Collaboration, Splits…", "Copy files
into library"; coverage rows 5, 15, 18, 19).

Today the only way to move files is a `beets.command move` step (S-BEETS-COMMAND), which bypasses
the journal and can't be undone (C-CHANGESET non-goal, C-JOURNAL non-goal). The author imports in
place, reviews, and only then moves albums into the library, and relies on undo (decision
2026-11-18). So moving files becomes a first-class, journaled operation:

- Destinations are computed by beets itself (`Item.destination()` with the profile's beets
  `paths:` formats, `replace:` rules and template functions). Rifferr doesn't invent a path
  language (non-negotiable 2: check what beets already does).
- The plan is a **FileChangeSet**: the list of `(item, old path, new path)` moves for one album.
  It follows the same propose → review/apply → journal → undo path as tag ChangeSets
  (non-negotiable 3). The journal is written before any file is touched.
- Undo never removes the only copy of a file (author's rule, 2026-11-14).

Architecture §2 (ChangeSets, journal), §3 (step types), §8 (backend protocol).

This spec amends three Implemented specs; the amendments are listed under *Tasks* and only take
effect when this spec is Approved:
- C-CHANGESET: drop the non-goal "File operations (move, rename, delete) in ChangeSets" for
  move/copy (delete stays a non-goal); add the `files` change kind.
- C-JOURNAL: file effects, their per-file states, crash recovery and undo rules.
- C-BACKEND: a `move` capability with `plan_moves` and `apply_moves`.

## Behavior
### Parameters
- LIBRARY-MOVE-1: The step MUST accept `mode` (`move` or `copy`, default `move`), `apply`
  (C-WORKFLOW WORKFLOW-3 policy, default `review`), `when` (a backend query narrowing the run's
  albums, default all), `paths` (optional ordered list of `{query, format}` overriding the
  profile's beets `paths:` for this step; first matching query wins, `default` last, exactly as
  beets' `paths:` resolves) and `directory` (optional destination root overriding beets'
  `directory`; `{{ }}` interpolated), `artifacts` (ordered rules for untracked files in the
  album folder that travel with the album: each a glob string, or `{match, to}` where `match`
  is a glob list matched case-insensitively relative to the album folder and `to` is a
  sub-folder of the destination album folder; the first matching rule wins; default `["*"]`,
  i.e. every untracked file at its current relative path; `[]` moves audio and art only) and
  `stamp` (optional
  `{field, format}`: a field set to the move date, default format `%Y-%m-%d`).
- LIBRARY-MOVE-2: `paths` formats and queries MUST be beets template / query syntax evaluated by
  the backend, never `{{ }}` interpolation (Conventions: never mix evaluators). `{{ }}` is allowed
  only in `directory`.

### Planning
- LIBRARY-MOVE-3: For each selected album, the step MUST ask the backend for the destination of
  every item (and of the album's art file, if any) and propose one FileChangeSet per album
  containing only files whose destination differs from their current path. An album with no
  differing files proposes nothing and logs "already in place".
- LIBRARY-MOVE-4: Planning MUST NOT touch the filesystem except to read (`stat`, existence).
- LIBRARY-MOVE-5: Planning MUST stop the album as a conflict (LIBRARY-MOVE-5a) when: a destination lies
  outside `directory` after resolving `..` and symlinks; two items of the run map to the same
  destination; a destination file already exists and is not the item's own current file; or an
  album's destination folder already exists and is not that album's own current folder (no
  merging into another album's folder, even when no file name collides). The album goes to the
  review queue as a conflict naming the item or folder and the colliding path. Rifferr never
  overwrites and never adds beets' `.1` suffixes.
- LIBRARY-MOVE-5a: A conflicted album (LIBRARY-MOVE-5, or a path format that fails for one of its
  items) MUST wait in the review queue as a *move conflict* (queue kind `move`) that holds only
  its lane (LANES-14), listing each conflict and the source and destination folders. It keeps the
  step's resolved `with:` (mode, paths, directory, artifacts, stamp). Decisions:
  `POST /api/move-conflicts/{id}/decision {decision: retry|fail}`.
  - `retry` (`202 {job_id}`) plans the album again in the worker with the kept settings. Still
    conflicted: the item stays pending with the new conflicts and the job fails naming them.
    Already in place: the item resolves and the lane continues. Otherwise the FileChangeSet is
    applied at once through the journal (the retry is the review, as a cover choice is,
    COVERS-SEARCH-21) and the lane continues; a file that then fails fails the lane
    (LIBRARY-MOVE-10).
  - `fail` (`202 {job_id: null}`) ends the lane `failed` at once.
  The item is cancelled with its run, notifies like other queue items (NOTIFY-8), and emits
  `move.requested` / `move.resolved` (added 2026-11-18, author: question 3).
- LIBRARY-MOVE-6: Untracked files in the album folder matching `artifacts` (rip logs, scans,
  `.cue`, …) MUST be planned as file effects of kind `artifact`, and are hashed, journaled,
  moved and undone exactly like audio files. Files in sub-folders count. A plain glob rule keeps
  the file's path relative to the album folder; a `{match, to}` rule puts it in `to` under the
  destination album folder, keeping only its file name (name collisions are conflicts,
  LIBRARY-MOVE-5). The album's art file (beets `artpath`) is kind `art`, never an artifact, so
  artifact rules don't match it. `to` MUST be a relative path that stays inside the album
  folder. Files matching no rule stay behind for `files.cleanup`.
- LIBRARY-MOVE-6a: Because the step moves files itself, beets `move`/import hooks (e.g. the
  third-party `copyartifacts3` plugin) do not fire. `artifacts` replaces that plugin for this
  step; the safety report notes when the profile's beets config enables it. The safety report
  MUST list every `library.move` step as a `file_move` finding naming the mode, the destination
  directory and the artifact rules, plus one `file_move` note per `copyartifacts` /
  `copyartifacts3` plugin the profile's beets config enables (including `include:` files).
- LIBRARY-MOVE-6b: With `stamp`, the FileChangeSet MUST also carry an album-level tag change
  setting `stamp.field` to the apply date in `stamp.format`. It is journaled and undone with the
  moves, and written only once the album's files are all `done`.

### Apply
- LIBRARY-MOVE-7: Applying a FileChangeSet MUST first write a journal entry listing every
  planned file effect (`from`, `to`, `mode`, size and SHA-256 of the source) with state
  `planned`, then process the files one by one.
- LIBRARY-MOVE-8: Per file, `copy` MUST create the destination folder, copy to a temporary name
  in the destination folder, fsync, verify size and SHA-256 against the journal, rename to the
  final name, then update the library path. `move` does the same and then removes the source,
  only after the destination is verified. A same-filesystem `move` MAY use an atomic rename
  instead of copy + verify.
- LIBRARY-MOVE-9: After each file, the journal entry MUST record that file's state (`planned` →
  `copied` → `done`). The library's stored path changes only once the destination is verified.
- LIBRARY-MOVE-10: Any failure stops the album at that file. Files already `done` stay done; the
  album's lane fails with the journal entry marked `partial`, and the run log names every file
  and its state. Undo of a partial entry is allowed (LIBRARY-MOVE-13).
- LIBRARY-MOVE-11: Source folders left empty by a `move` MUST NOT be removed by this step
  (`files.cleanup` does that, S-FILES-CLEANUP).

### Recovery and undo
- LIBRARY-MOVE-12: On worker start, a journal entry left in `planned` or `copied` state MUST be
  reconciled from the filesystem: a verified destination plus an existing source becomes
  `copied`, a verified destination with no source becomes `done`, and a temporary file is
  removed. Nothing is ever deleted unless a verified copy exists elsewhere.
- LIBRARY-MOVE-13: Undo MUST build the inverse plan (each `to` back to `from`) and run it through
  the same apply path. Before removing any file, undo MUST verify that the other copy exists and
  its SHA-256 matches; otherwise that file is skipped and reported, and the album stays in the
  review queue as "undo incomplete".
- LIBRARY-MOVE-14: Undo of a `copy` MUST remove the library copy only if the original still
  exists with a matching hash; it then restores the library path to the original. Undo never
  deletes a file that is the only copy.
- LIBRARY-MOVE-15: Undo MUST refuse when a destination file changed since the move (hash
  differs), unless the user forces it for that file. The refused undo job fails and its result
  names the files in `changed_files` (undo is a job, so not an HTTP 409; amended 2026-11-18).
  The run page's undo report (REVIEW-QUEUE-15) lists them with a checkbox each and "Undo
  anyway", which queues the undo again with `{force: [paths]}` (also accepted by the journal
  entry undo route). A forced moved file goes back as it is now; for a forced copy the changed
  library copy is removed only when the unchanged original is still there (LIBRARY-MOVE-14).

### Review
- LIBRARY-MOVE-16: In the review queue, a FileChangeSet MUST show per album the old and new
  folder, and per file the old and new name, with partial accept per album (not per file, so an
  album never ends up split across folders).

## Interfaces
```python
class FileEffect(BaseModel):          # C-CHANGESET `files` change kind
    target: TargetRef                 # item, or album for its art file
    kind: Literal["item", "art", "artifact"]
    src: str                          # absolute path
    dst: str
    mode: Literal["move", "copy"]
    size: int
    sha256: str
    state: Literal["planned", "copied", "done", "skipped"] = "planned"

class LibraryBackend(Protocol):       # capability "move" (C-BACKEND amendment)
    def plan_moves(self, selection: Selection, *, mode: FileMode,
                   formats: Sequence[PathFormat] | None, directory: Path | None,
                   artifacts: Sequence[ArtifactRule]) -> list[MovePlan]: ...  # reads only
    def copy_file(self, effect: FileEffect) -> FileEffect: ...      # planned -> copied (or done)
    def remove_source(self, effect: FileEffect) -> FileEffect: ...  # copied -> done, dst verified
    def set_paths(self, effects: Sequence[FileEffect]) -> None: ... # library path update only
    def file_sha256(self, path: str) -> str | None: ...
    def file_stat(self, path: str) -> tuple[int, str] | None: ...   # size, sha256 (LIBRARY-MOVE-15)
    def reconcile_file(self, effect: FileEffect) -> FileEffect: ... # recovery, removes temp files

class MovePlan(BaseModel):            # one album
    album: TargetRef; label: str; src_dir: str; dst_dir: str
    files: list[FileEffect]; conflicts: list[str]   # LIBRARY-MOVE-5; empty = can move

class ChangeSet:                      # C-CHANGESET amendment
    files: list[FileEffect] = []      # accepted or rejected as a whole
    stamp: Stamp | None = None        # the stamp field change gets the apply date

class PathFormat(BaseModel):
    query: str                        # beets query, or "default"
    format: str                       # beets path format
```

Workflow YAML:
```yaml
- id: into-library
  uses: library.move
  with:
    mode: copy
    directory: "{{ vars.library }}"
```

## Examples
The author's foobar2000 pattern (2026-11-18), for the profile's beets config:

Regular albums are sorted by original year inside the artist folder; splits and compilations
put the year after the album name (author, 2026-11-18). Proposed beets `paths:` (needs the `the`
plugin and the `ifgreater` and `unpad` template functions, C-TEMPLATE-FUNCTIONS; `albumkind` is the
flexible attribute the classification rules set):

```yaml
plugins: [the]
the: { a: no, format: "{0}, {1}" }
paths:
  "albumkind::^(split|compilation)$": "%the{$albumartist}/$album%if{$original_year, ($original_year)} [%if{%ifgreater{$bitdepth,16,1}, 24-bit }$format]/%ifgreater{$disctotal,1,Disc %unpad{$disc}/}$track. $title"
  default: "%the{$albumartist}/%if{$original_year,($original_year) }$album [%if{%ifgreater{$bitdepth,16,1},24-bit }$format]/%ifgreater{$disctotal,1,Disc %unpad{$disc}/}$track. $title"
```

- `%the{}` changes folder names only; tags keep "The Beatles" (decision 2026-11-18).
- `$format` as beets reports it ("AAC", "ALAC" for M4A) is wanted (2026-11-18).
- 24-bit releases get "[24-bit FLAC]" in folder names only (B-046, 2026-11-18).

`albumartist=The Beatles, album=Abbey Road, original_year=1969, format=FLAC, bitdepth=16,
disctotal=1, track=1, title=Come Together` →
`Beatles, The/(1969) Abbey Road [FLAC]/01. Come Together.flac`.

The reference workflow's step:
```yaml
- id: into-library
  uses: library.move
  with:
    directory: "{{ vars.library }}"
    stamp: { field: date_added }        # checklist "DATE ADDED", the day it entered the library
    artifacts:
      - { match: ["*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.tif", "*.tiff", "*.bmp"], to: Art }
      - "*"                             # everything else keeps its place
```

Album folder `cover.jpg` (beets art), `back.jpg`, `scans/cd.png`, `rip.log` → destination
`cover.jpg`, `Art/back.jpg`, `Art/cd.png`, `rip.log`.

## Extensibility
- Path formats, `replace:` rules and template functions come from the profile's beets config
  and `rifferr.template_functions` plugins; the step adds no path syntax of its own.
- Another backend (e.g. FilesBackend) implements the `move` capability; the engine-side apply,
  journal and undo stay fixed.

## Security & safety
- No trust needed: the step runs no user code. Destinations are confined to `directory`
  (LIBRARY-MOVE-5), which the static safety report lists.
- Never overwrites, never deletes without a verified copy, never follows a symlink out of
  `directory`.
- Paths are logged; file contents and secrets are not.

## Non-goals
- Deleting files (stays with `files.cleanup`).
- Per-file partial accept.
- Merging an album into an existing folder.
- Converting audio for devices (iPod sync).

## Open questions
Resolved 2026-11-18 (author): splits/comps put the year after the album, regular albums before
it; `$format` as is; 24-bit marked in folder names; artifacts always travel (user setting,
default all); `move` is the default and an existing destination album folder is a conflict;
DATE ADDED is the day the album moves into the library (`stamp`); regular albums read
"(1969) Abbey Road [FLAC]"; the checklist's "Splits/Collabs first" order is dropped; `albumkind`
with `split`, `collab`, `soundtrack`, `compilation` is accepted; artwork other than the album's
cover goes in an `Art` sub-folder (artifact rules).

1. "24-bit" currently means any bit depth above 16 (author: "probably"). Revisit if a 20-bit or
   32-bit float release should be labelled differently.
2. Resolved 2026-11-18 (author): match foobar2000 (`Disc 1/`). beets pads `$disc` in path
   formats, so the reference pattern uses the built-in `%unpad{$disc}` (TEMPLATE-FUNCTIONS-7).
3. Resolved 2026-11-18 (author): conflicts are review-queue items with retry (LIBRARY-MOVE-5a).

## Tasks
- [x] C-CHANGESET amendment: `files` change kind; narrow the file-operation non-goal to deletes.
- [x] C-JOURNAL amendment: file effects, per-file states, recovery, undo rules (LIBRARY-MOVE-7..15).
- [x] C-BACKEND amendment: `move` capability; contract tests for `Item.destination()` with path
  formats, `replace:`, the `the` plugin and Rifferr template functions.
- [x] FakeBackend `move` capability on an in-memory filesystem.
- [x] Step, apply path, recovery, undo; tests per behavior ID.
- [x] Move conflicts in the review queue (LIBRARY-MOVE-5a): engine, worker retry job, API, UI.
- [x] LIBRARY-MOVE-6a: safety report notes `copyartifacts`/`copyartifacts3` in the profile's beets config.
- [x] LIBRARY-MOVE-15: "force" for changed files (engine, undo routes, run page control).
- [x] Review queue: FileChangeSet view (LIBRARY-MOVE-16) and Playwright test.
- [x] `examples/workflows/foobar-style.yaml` and `docs/coverage.md` rows 5, 13, 15, 17, 18, 19.
