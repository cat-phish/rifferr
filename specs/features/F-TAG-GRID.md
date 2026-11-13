# F-TAG-GRID: Library and tag grid
Status: Approved
Milestone: M1

## Context
The tag grid is where a person looks at the library and fixes tags by hand
(architecture §4, §11; M1 row "library/tag grid"). The script preview grid already
exists on the script editor page (S-TAGSCRIPT TAGSCRIPT-22); this spec covers the
library browser and manual edits.

Every change is a ChangeSet (non-negotiable 3), so a manual edit is a ChangeSet with
source `manual` / `ui`, no run and no step (CHANGESET-1), applied by the engine through
the journal like any other, and undoable (C-JOURNAL). The grid only reads through the
backend's snapshots (non-negotiable 2), in the profile's read-only worker so it answers
while a run imports (WORKER-11, as the script preview does).

Manual ChangeSets have no run, so their `changeset.*` events have no stream yet
(C-CHANGESET, B-030). The UI never polls (CLAUDE.md conventions), so this spec adds a
profile event stream.

## Behavior
- TAG-GRID-1: The page `/library` (nav item "Library") MUST show a query box (beets query syntax, with a link to the beets query docs), a scope switch (`album` | `item`), and the matching albums or items in a virtualized grid. An empty query lists the whole library, which is read-only browsing.
- TAG-GRID-2: `GET /api/profiles/{profile}/library?query=&scope=&limit=` MUST answer from the profile's read-only worker with snapshots (C-BACKEND), `truncated`, and the field names seen, and MUST NOT write anything. `limit` defaults to 1000 and is at most 10000. A query error MUST answer `400 bad_query` with the backend's message. Timeout and supersede behave as TAGSCRIPT-21 (`504 query_timeout`, `409 superseded`).
- TAG-GRID-3: Columns MUST be chosen from the field names seen, per scope. The default columns come from the profile setting `ui.grid_columns.{album,item}`, falling back to a default in the UI (open question 1, resolved). Column choice and widths changed in the UI MUST be remembered per browser and profile, not written to the profile file.
- TAG-GRID-4: In album scope a row MUST expand to show the album's items; the item rows are editable like item-scope rows.
- TAG-GRID-5: A cell MUST be editable in place (double-click or Enter). Multi-value fields MUST be edited as a list, one value per entry, never by splitting text on a separator (open question 3). An edit MUST NOT change the library yet: it becomes a *pending edit*, shown in the cell as before → after.
- TAG-GRID-6: With several rows selected, "Set field" (field name and one literal value) and "Delete field" MUST add a pending edit to each selected row. Values are literal text; beets templates belong to tag scripts (TAG-GRID-12).
- TAG-GRID-7: Pending edits MUST be checked by the server before they're applied: `POST /api/profiles/{profile}/changes/check` coerces each value to its field's type (`backend.coerce`, BACKEND-11) and runs `backend.check` (CHANGESET-2) in the read-only worker, and answers the problems per target and field. Cells with problems MUST show the message and block Apply until fixed or discarded.
- TAG-GRID-8: Apply MUST send all pending edits in one request, `POST /api/profiles/{profile}/changes {targets, rationale?}`. The server MUST turn them into ChangeSets with source `manual` / `ui`, one per album (CHANGESET-14), with each field's `old` taken from the snapshot the user edited (so changes made meanwhile come back as conflicts, CHANGESET-8), and queue one worker job that applies them through the journal (CHANGESET-13). Manual ChangeSets apply immediately; they don't go to the review queue (open question 2). The response gives the ChangeSet ids and the job id.
- TAG-GRID-9: `GET /api/profiles/{profile}/events` (SSE) MUST carry the `changeset.*` events of the profile's ChangeSets that have no run, persisted before they're sent, with `Last-Event-ID` replay as API-8. This closes B-030.
- TAG-GRID-10: After Apply, the grid MUST show each ChangeSet's outcome from the event stream: applied cells refresh from a new query; conflicted cells show the current value next to the one the user wanted, with "apply on top" (a new edit whose `old` is the current value) and "discard". Tag-write failures (CHANGESET-10) MUST be shown on their rows.
- TAG-GRID-11: A "Recent edits" panel MUST list the profile's manual ChangeSets, newest first, with their state, and offer Undo for applied ones, which queues the C-JOURNAL undo job for its journal entry (JOURNAL-4, -5) and shows the result, including a conflict that stopped it.
- TAG-GRID-12: "Preview a script on these" MUST open the script editor (`/scripts`) with the grid's current query filled in.
- TAG-GRID-13: While the profile's worker runs a job (e.g. an import), Apply MUST still be accepted; the edit job waits its turn and the UI MUST say it's queued behind the running job (open question 5).
- TAG-GRID-14: Values MUST be shown as text, never as HTML.
- TAG-GRID-15: A Playwright test MUST cover: query, expand an album, edit a cell, check, apply, see it applied, undo it, against the e2e temp profile.

## Interfaces
```python
class LibraryPage(BaseModel):
    albums: list[AlbumSnapshot]      # item scope: albums with only the matching items (BACKEND-5)
    fields: list[str]                # field names seen, sorted
    truncated: bool

class ManualChange(BaseModel):       # what the UI sends
    target: TargetRef
    changes: list[ProposedFieldChange]   # old = the value the user saw

class ChangesRequest(BaseModel):
    targets: list[ManualChange]
    rationale: str | None = None

class ChangesCreated(BaseModel):
    changesets: list[str]
    job_id: str

class CheckProblem(BaseModel):
    target: TargetRef; field: str; message: str
```
Routes (all under `/api/profiles/{profile}`): `GET /library`, `POST /changes/check`,
`POST /changes`, `GET /changes?source=manual&limit=` (recent edits with state and
journal entry id), `POST /journal/{entry_id}/undo`, `GET /events` (SSE).
Profile file: optional `ui.grid_columns: {album: [...], item: [...]}`.
Read-only worker request kinds: `library.query`, `changes.check`.

## Examples
Query `albumartist:Fixture`, album scope → one row, `Fixture Artist - Preview Sessions`.
The user sets `year` to `2001` on it and `genre` on both items via "Set field"
→ check passes → Apply → one ChangeSet (album + 2 items) → `changeset.applied`
on the profile stream → the row refreshes with year 2001. Undo → the undo job
reverts both fields → `changeset.undone`.

Someone runs `beet modify year=1999` between load and Apply → the ChangeSet
conflicts; the cell shows "now 1999, you wanted 2001" with "apply on top".

## Extensibility
- Columns and default columns are data (profile setting), not code.
- The grid only uses snapshots, `coerce` and `check`, so any backend with `query` and `apply` works.

## Security & safety
- Mutating routes are JSON-only behind the F-SECURITY middleware.
- The grid can only change fields through ChangeSets the backend checks; read-only and computed fields are refused by `check`.
- Values are rendered as text (TAG-GRID-14).

## Non-goals
- Editing file names or moving files (use `beets.command move`).
- Templates or expressions in grid edits (tag scripts do that).
- Sorting or filtering beyond the beets query (beets queries can sort: `year+`).
- Album art in the grid (F-COVER-PICKER, M3).
- The review queue and dashboard (F-REVIEW-QUEUE).

## Open questions
Resolved 2026-11-12 (author: "go with your recommendations, approved"):
1. ~~Default columns?~~ A built-in UI default of beets' standard fields (album scope
   `albumartist, album, year, albumtype, genre`; item scope
   `track, title, artist, album, length`), overridden by `ui.grid_columns` (TAG-GRID-3).
2. ~~Apply immediately or review first?~~ Immediately (TAG-GRID-8).
3. ~~Multi-value fields?~~ Edited as a list of entries; no `;` splitting (TAG-GRID-5).
4. ~~`@tanstack/react-table`?~~ Not added; Mantine's table plus `@tanstack/react-virtual`.
5. ~~Apply while a run is active?~~ Queue behind it (TAG-GRID-13).
6. ~~Undo scope?~~ Manual ChangeSets only (TAG-GRID-11).

## Tasks
- [ ] read-only worker: `library.query`, `changes.check`
- [ ] routes: library, changes/check, changes, recent changes, journal undo
- [ ] worker job applying manual ChangeSets; profile event stream (B-030)
- [ ] `/library` page: query, scope, virtualized grid, column picker, expand albums
- [ ] pending edits, bulk set/delete, check, apply, outcome, conflicts
- [ ] recent edits panel with undo; script hand-off
- [ ] Playwright test (TAG-GRID-15)
