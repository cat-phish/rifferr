# F-DASHBOARD: Queue dashboard
Status: Draft
Milestone: M2

## Context
Architecture §2a: "the home screen is the queue: the inbox status, runs in progress,
items needing review, and recent imports with one-click undo." M2 is done when music
dropped in the inbox imports unattended and an ambiguous album waits in review without
blocking the others. The dashboard is where the user checks on that afterwards, often
days later, from a notification or by opening the app.

Everything it shows already exists in pieces:

- Watch triggers and dedup records (F-TRIGGERS): which folders are watched, which
  inputs were imported by which run, and the trigger state.
- Runs and their states (F-API, F-UI-SHELL), including `waiting_review` (C-LANES).
- The review queue (F-REVIEW-QUEUE) with its four item kinds: `changeset`, `gate`,
  `import` (F-IMPORT-UI) and `cleanup` (S-FILES-CLEANUP).
- Imported albums carry the run's id (`RUN_MARKER`, C-BACKEND).

Missing are a screen that puts these together on `/`, the inbox listing, undo of an
import (C-JOURNAL leaves it to "the M2 dashboard"), and two queue changes other specs
deferred to this one: per-album decisions on a multi-album gate (C-LANES non-goal) and
several decisions on one screen (F-IMPORT-UI non-goal). F-UI-SHELL already settled that
the dashboard takes `/` and `/runs` stays the runs list.

## Behavior
Inbox:
- DASHBOARD-1: `GET /api/profiles/{profile}/inbox` MUST list, for each armed or failing `watch` trigger, the watched folder and its candidate inputs (the same top-level entries the watcher would fire for, F-TRIGGERS), each with a state: `settling` (changed less than `settle` ago), `waiting` (settled, not started yet, or queued behind another run), `running`, `waiting_review`, `imported` (the dedup record's fingerprint matches and its run succeeded or is waiting), `failed` (the last run for it failed or was cancelled), or `changed` (imported before, changed since). Each entry carries its last run id when there is one.
- DASHBOARD-2: The inbox listing MUST read only the filesystem and the app DB (never beets), MUST stay inside the watched folder (symbolic links out of it are listed as `outside` and not followed), and MUST cap the entries per folder (open question 3), saying how many were left out.
- DASHBOARD-3: An entry in `failed` or `changed` MUST offer "Import again", which fires the trigger's workflow for that input with `force` (F-TRIGGERS dedup override), going through the same trust and concurrency checks as a manual run.

Runs in progress:
- DASHBOARD-4: The dashboard MUST show the profile's queued, running and `waiting_review` runs, newest first, with workflow, trigger, input, state, started time, and for `waiting_review` the number of waiting lanes and queue items. Rows link to the run page.

Items needing review:
- DASHBOARD-5: The dashboard MUST show the number of waiting queue items per kind and the oldest few items (open question 4), each linking to `/review?item=<id>`.

Recent imports:
- DASHBOARD-6: `GET /api/profiles/{profile}/imports?limit=` MUST list the most recent runs that imported albums, newest first, each with its albums (label, id, item count) as the read-only worker finds them by `RUN_MARKER`, and whether each album can still be undone.
- DASHBOARD-7: `POST /api/runs/{id}/import-undo {albums?: [id]}` MUST queue a worker job that removes those albums of the run (all of them when absent) from the library, after first undoing the run's applied ChangeSets for them (JOURNAL-6, lane-scoped as LANES-11). It answers `202 {job_id}`; `409` while the run is queued or running. Files are handled as open question 1 decides.
- DASHBOARD-8: The import undo MUST write a journal entry per album before removing it (non-negotiable 3), stop at the first album it can't remove, and report what it removed and why it stopped, in the job's `result` (`job.finished`, F-REVIEW-QUEUE).
- DASHBOARD-9: After an import undo, the dedup record for the run's input MUST be cleared, so the inbox shows the input as `waiting` again if it is still there (open question 2).
- DASHBOARD-10: "Undo import" MUST ask for confirmation naming the albums and saying what happens to their files.

Queue changes:
- DASHBOARD-11: `POST /api/reviews/{gate_id}/decision` MUST accept an optional `albums: [id]`: the decision then applies only to those albums' lanes. Lanes not named stay waiting on the gate, and the gate stays `pending` until every lane is decided (open question 5). Without `albums` it behaves as now (REVIEW-QUEUE-6).
- DASHBOARD-12: A review gate card MUST offer "Continue" and "Reject" per album as well as for all.
- DASHBOARD-13: `/review` MUST let the user select several items of the same kind and decide them together (accept/reject ChangeSets, continue/reject gates, keep/delete cleanups). Import decisions stay one at a time: each needs its own candidate choice. Each selected item is decided as if alone; the page reports the ones that failed.

Page and updates:
- DASHBOARD-14: `/` MUST show the dashboard for the selected profile with the four sections in this order: Needs review, Runs in progress, Inbox, Recent imports. The nav item "Runs" moves to `/runs`.
- DASHBOARD-15: The dashboard MUST update from `GET /api/events` without polling. Run, review, import and cleanup events already arrive there; the trigger manager adds `inbox.changed {profile, workflow, path}` when a watched folder's candidates change state (settle started, settled, fired), coalesced to one per input per second.
- DASHBOARD-16: Paths, labels and messages MUST be shown as text, never HTML.
- DASHBOARD-17: A Playwright test MUST cover, against the e2e temp profile: an inbox folder shown as `waiting`, a run started from it shown in progress and then waiting for review, the waiting item reached from the dashboard, and an import undone from Recent imports.

## Interfaces
```python
class InboxEntry(BaseModel):
    path: str                     # relative to the watched folder
    state: Literal["settling", "waiting", "running", "waiting_review",
                   "imported", "failed", "changed", "outside"]
    changed_at: datetime | None
    run_id: str | None

class InboxFolder(BaseModel):
    workflow: str
    folder: str                   # resolved watch path
    trigger_state: Literal["armed", "disabled", "error"]
    detail: str | None
    entries: list[InboxEntry]
    omitted: int

class ImportedAlbum(BaseModel):
    id: str; label: str; items: int
    undoable: bool; reason: str | None     # why not, e.g. "already removed"

class RecentImport(BaseModel):
    run_id: str; workflow: str; input: str | None
    finished_at: datetime | None
    albums: list[ImportedAlbum]

class ImportUndoBody(BaseModel):
    albums: list[str] | None = None

class GateDecisionBody(BaseModel):          # REVIEW-QUEUE-6, additive
    decision: Literal["continue", "reject"]
    note: str | None = None
    albums: list[str] | None = None
```
Routes: `GET /api/profiles/{profile}/inbox`, `GET /api/profiles/{profile}/imports`,
`POST /api/runs/{id}/import-undo`. Read-only worker request kind `imports.recent`.
Job kind `import_undo`. Backend protocol: `remove(selection, delete_files)`
(C-BACKEND, additive; the beets backend uses beets' library `remove`). Event
`inbox.changed`. UI routes: `/` (dashboard), `/runs` unchanged.

## Examples
Inbox `/music/inbox` with the reference workflow's watch trigger:

```
Inbox — foobar-style (armed, settle 2m)
  Artist A - Album 1       imported        run 7f3…
  Artist B - Album 2       waiting_review  run 81a…   (1 import decision)
  Artist C - Album 3       settling        changed 40 s ago
  Artist D - Album 4       failed          run 6c0…   [Import again]
```

Recent imports: run 7f3… imported "Artist A - Album 1" (12 tracks). "Undo import"
asks: "Remove Artist A - Album 1 (12 tracks) from the library? Its tag changes from
this run are undone first. <files sentence per open question 1>." The job undoes the
run's ChangeSets for the album, writes a journal entry, removes the album, clears the
dedup record; the inbox shows "Artist A - Album 1" as `waiting` if the folder is still
there.

A gate holds three albums; the user continues A and B and rejects C:
`{decision: continue, albums: [A, B]}` resumes those lanes; the gate stays pending for
C; `{decision: reject, albums: [C]}` applies `on_reject` to C's lane and resolves the gate.

## Extensibility
- Inbox states come from triggers, so any trigger kind with a folder can list one later
  (webhook inputs have no folder and are left out).
- Removal goes through `LibraryBackend.remove`, so another backend supplies its own.
- Dashboard sections are separate components fed by separate routes; later milestones
  (cover picks, AI suggestions) appear through the queue's item kinds.

## Security & safety
- Import undo is the first path that removes albums from the library. It runs in the
  worker as a job, journals before it acts, and only touches albums carrying that run's
  `RUN_MARKER`.
- Deleting files (if open question 1 allows it) only ever touches files the backend
  reports as belonging to those albums, inside the library directories (`library_dirs`).
- The inbox route lists names only, never file contents, and never leaves the watched
  folder.
- All new mutating routes are JSON-only behind the F-SECURITY middleware.

## Non-goals
- Undoing `beets.command` or `shell` side effects (C-JOURNAL non-goal stands).
- Restoring files to the inbox after an import that moved them (open question 1).
- Editing triggers or workflows from the dashboard.
- Charts or statistics.
- A `rifferr` CLI for the dashboard or import undo.

## Open questions
1. Files on import undo. beets imports either copy (inbox keeps the originals) or move
   (the library holds the only copy). Options: (a) remove from the library database
   only and leave files where they are; (b) also delete the album's files from the
   library folder, only when the run's import copied (the originals are still in the
   inbox); (c) always offer "also delete files", off by default. Recommended: (b) —
   with copy imports, (b) leaves nothing behind and loses nothing; with move imports it
   behaves as (a) and the confirmation says the files stay in the library folder.
2. Clearing the dedup record after undo (DASHBOARD-9) means a watch trigger may import
   the folder again on its next scan if it is still in the inbox. Recommended: clear it,
   but the input waits for an explicit "Import again" rather than firing by itself
   (state `waiting`, marked "undone").
3. Inbox cap: recommended 200 entries per folder.
4. Needs review: recommended the oldest 5 items plus counts, linking to `/review`.
5. Per-album gate decisions (DASHBOARD-11): include now, or leave whole-gate only?
   Recommended: include; C-LANES and F-IMPORT-UI both deferred it to this spec.
6. Multi-select on `/review` (DASHBOARD-13): include now, or defer to the backlog?
   Recommended: include, same reason.
7. New spec vs. editing F-REVIEW-QUEUE: recommended a new spec (this one), since
   F-REVIEW-QUEUE is Implemented; it gets a note pointing here.

## Tasks
- [ ] backend protocol `remove` + beets backend and contract test
- [ ] `import_undo` worker job (journal, ChangeSet undo first, dedup record)
- [ ] read-only worker request `imports.recent`
- [ ] routes: inbox, recent imports, import undo
- [ ] per-album gate decisions
- [ ] `inbox.changed` events from the trigger manager
- [ ] dashboard page on `/`; nav "Runs" to `/runs`
- [ ] multi-select decisions on `/review`
- [ ] Playwright test (DASHBOARD-17)
