# S-LIBRARY-PRUNE: Drop library entries whose files are gone
Status: Implemented
Milestone: M2

## Context
When someone deletes album files by hand (the author's test loop: empty the staging and
library folders, refill the inbox with the same sample), beets still lists those albums.
Every re-import then counts as a duplicate of an album that no longer exists. Kept as a
duplicate, the new album's path also picks up a `%aunique{}` disambiguator it doesn't
need. beets' `update` command drops such entries, but it also re-reads the tags of every
other file in its query. That is a library-wide change a `beets.command` step must not
make (BEETS-COMMAND-4 limits it to the run's albums).

This step drops only the entries whose files are all gone. It uses the backend's existing
`remove_album(album_id, delete_files=False)` (F-DASHBOARD, `remove` capability) and its
removal records (`album_removals`), so it never touches a file. It is meant to run before
`beets.import` (architecture §5 steps, §8 backend protocol).

## Behavior
- LIBRARY-PRUNE-1: The step `library.prune` MUST find the albums matching `query` (a beets
  query, default all albums) for which **every** item's file is missing from disk. Albums
  with only some files missing MUST NOT be removed; they are listed in the step's output
  and the run log as `partial`.
- LIBRARY-PRUNE-2: Finding MUST only read the filesystem (existence checks). A path that
  can't be checked (permission error, missing mount) counts as present, so an unmounted
  drive never makes its albums look deleted. If every item of every matching album is
  missing and that is more than `max_albums` (default 50), the step MUST fail without
  removing anything ("N albums look deleted; is the library's drive mounted?").
- LIBRARY-PRUNE-3: With `apply: review` (the default), the albums to drop MUST wait in
  the review queue as one item that holds no album lane, listing each album (label, path,
  item count). It can be accepted or rejected as a whole. With `apply: auto` they are
  dropped without review.
- LIBRARY-PRUNE-4: Each album MUST be dropped with `remove_album(album_id,
  delete_files=False)`, writing an `album_removals` record (`pending` before and
  `removed` or `failed` after, with the album's snapshot) so the removal is journaled.
  The step MUST NOT delete, move or write any file.
- LIBRARY-PRUNE-5: The step's outputs MUST be the counts `removed`, `partial` and
  `skipped` (step outputs are scalars), and the run log MUST name each album. `skipped`
  are albums whose files came back, or that left the library, before the removal.
  With review, the output also has `decision` (the decision id), and the counts are the
  decision's `result` once applied.
- LIBRARY-PRUNE-6: Right before removing, each album's files MUST be checked again; an
  album with any file present by then is skipped (LIBRARY-PRUNE-5).
- LIBRARY-PRUNE-7: The static safety report MUST list the step as `destructive`:
  "removes library entries whose files are missing (files are not touched)". With
  `apply: auto` it is also listed as `auto_apply`.

## Interfaces
```yaml
- id: prune
  uses: library.prune
  apply: review            # or auto
  with:
    query: ""              # beets query; default all albums
    max_albums: 50
```

Outputs: `removed`, `partial`, `skipped` (counts), plus `decision` with review. Queue kind:
`prune` (REVIEW-QUEUE-1, additive), decided by
`POST /api/prunes/{id}/decision {decision: accept | reject, note?}`. The backend method
is the existing `remove_album`. It needs no new protocol method, only a read of the
albums' item paths (snapshots already have `path`).

## Examples
The author's test profile, before the import:
```yaml
steps:
  - id: prune
    uses: library.prune
    apply: auto
  - id: import
    uses: beets.import
    ...
```
Library has A (all 12 files deleted), B (3 of 10 deleted), C (all present). Result:
A removed from the library, B listed as partial, C untouched; no file changes.

## Extensibility
Any backend with the `remove` capability supports the step. Without it, the step fails
at validation with "backend cannot remove albums".

## Security & safety
- Files are never touched (LIBRARY-PRUNE-4). Only library entries go, and only those whose
  files are all gone.
- An unreadable or unmounted path is treated as present, and the `max_albums` limit stops
  a mass removal (LIBRARY-PRUNE-2).
- Review is the default. `apply: auto` is shown in the safety report (LIBRARY-PRUNE-7).

## Non-goals
- Re-reading tags from changed files (that is beets' `update`, via `beets.command` on the
  run's albums).
- Removing single items from albums that are partly gone.
- Restoring dropped entries. The `album_removals` record keeps the snapshot, but an undo
  that re-adds entries is not part of this spec.

## Open questions
1. ~~Name: `library.prune` or `beets.prune`?~~ Resolved 2026-11-21 (author): as recommended. Recommendation: `library.prune`, since it goes
   through the backend protocol like `library.move`.
2. ~~Default `apply`?~~ Resolved 2026-11-21 (author): as recommended. Recommendation: `review`. The author's test profile sets `auto`.
3. ~~`max_albums` default 50?~~ Resolved 2026-11-21 (author): as recommended. Recommendation: yes. A test profile can raise it.
4. ~~Should singletons (items without an album) be pruned too?~~ Resolved 2026-11-21 (author): as recommended. Recommendation: not in this
   spec. The import flow is album-based.

Implementation notes (filled in while building):
- Queue kind `prune` (QueueItem `prune_decision`), lane hold kind `prune`, job kind
  `prune_apply`, events `prune.requested` and `prune.resolved`. Decisions live in
  `prune_decisions` (migration 017); `album_removals` gains a `snapshot` column.
- "Can't be checked": a permission error, or the backend library folder holding the path
  is missing. Paths outside the library folders are checked as they are.
- The review item holds the pass like a cleanup decision (LANES-16): before the import
  there are no album lanes, so it waits in its own pending lane.

## Tasks
- [x] Step model, finder (pure part over snapshots plus an injected existence check)
- [x] Review item kind `prune`, decision route, card
- [x] Removal through `remove_album(delete_files=False)` with `album_removals` records
- [x] Safety report findings
- [x] Tests per behavior ID; schemas and TS client regenerated
- [x] Add it to the author's test profile only (not the example workflow)
