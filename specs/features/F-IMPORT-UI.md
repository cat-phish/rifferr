# F-IMPORT-UI: Import decisions in the review queue
Status: Draft
Milestone: M2

## Context
Architecture §2a ("Unattended policy") and §3 (`beets.import`): `mode: auto` applies
strong matches, and every ambiguous album becomes an **import decision** in the review
queue. Only that album's lane waits (C-LANES LANES-14). `mode: interactive` asks about
every album. Reference checklist line 2 ("Run `beet import` … and try to match albums or
skip") is the human half of this. S-BEETS-IMPORT implemented `mode: quiet` only and left
`auto` and `interactive` to this spec (BEETS-IMPORT-8).

beets asks import questions as terminal prompts inside one synchronous `ImportSession`
(architecture §8). An unattended import can't hold that session open for days while it
waits for an answer. So this spec splits each album's import into two beets sessions:

1. **Lookup** (during the step): the session gets beets' candidates for each album.
   It applies a strong match at once. For any other album it stores the candidates as a
   decision and skips the album, leaving its files where they are.
2. **Apply** (after the decision, in a worker job): a new session imports that one
   folder with the chosen action. To pick a candidate it looks the candidate up again by
   its id (beets' `search_ids`), so nothing from the first session has to be kept alive.

Prior art: beets-flask (MIT, architecture §1) also keeps preview and import separate. Its
previews hold candidates with ids, distances and track mappings, and manual searches add
candidates to a task by `search_ids`, artist or album. Its auto-import threshold falls back
to beets' `match.strong_rec_thresh`. Rifferr does the same, but through workflows, lanes
and the review queue.

This spec amends S-BEETS-IMPORT (BEETS-IMPORT-8), F-REVIEW-QUEUE (a third queue item kind)
and C-LANES (a `skipped` lane state, open question 4).

## Behavior

### Modes
- IMPORT-UI-1: `beets.import` MUST accept `mode: auto` and `mode: interactive` (amends BEETS-IMPORT-8).
  - In `auto`, an album with a strong recommendation (beets' own `match.strong_rec_thresh`, not a Rifferr setting) MUST be imported with its best candidate, as in quiet mode. Every other album MUST become an import decision.
  - In `interactive`, every album MUST become an import decision, strong matches included.
  - `quiet` is unchanged (BEETS-IMPORT-4).
  - The default mode is open question 1.
- IMPORT-UI-2: In both modes the lookup MUST use the user's beets config and plugins (metadata sources, `match.*` weights, `max_candidates`). Rifferr MUST NOT reorder, filter or rescore beets' candidates.
- IMPORT-UI-3: A duplicate found while importing with `duplicate_action: ask` MUST become an import decision of kind `duplicate`, not a skip (quiet mode keeps skipping it, BEETS-IMPORT-4). Its choices are beets' own: `skip`, `keep` (both), `remove` (the old album) and `merge`.

### Lookup: creating a decision
- IMPORT-UI-4: For each album that needs a decision, the step MUST store an import decision in state `pending`. The decision holds:
  - the album folder path and its fingerprint (F-TRIGGERS `fingerprint`);
  - the folder's current items (path, format, length and their tags: artist, album, title, track, disc);
  - beets' candidates in beets' order. Each candidate has a stable id `<data_source>:<album id>`, its source, artist, album, year, country, label, catalog number, media, track count, distance (0–1) and beets' penalty names, plus a track mapping: per item the matched track, title changes, and the missing and unmatched tracks;
  - for kind `duplicate`, the existing albums it duplicates (snapshots);
  - the step's resolved `options` and `set` (after interpolation), so the later apply uses the same settings.
- IMPORT-UI-5: Every decision MUST get a pending lane `pending:<decision id>` (C-LANES LANES-14), labelled with the folder name and positioned at the step after `beets.import`, with an `import` hold whose ref is the decision id. The album's files MUST stay untouched until the decision is applied.
- IMPORT-UI-6: Creating a decision MUST emit `import.requested {decision_id, path, kind, candidates}` (the count) on the run's stream and on `/api/events`. The step's outputs MUST gain `decisions` (a count), and its log one line per decision ("waiting for an import decision").

### Deciding
- IMPORT-UI-7: `GET /api/review` MUST list pending import decisions as queue items of kind `import` (amends REVIEW-QUEUE-1). `GET /api/imports/{id}` MUST return the full decision.
- IMPORT-UI-8: `POST /api/imports/{id}/decision {action, candidate_id?, duplicate_action?, note?}` MUST accept these actions:
  - `candidate`: import with that candidate (its `candidate_id` must be listed);
  - `asis`: import with the current tags (beets' as-is);
  - `skip`: don't import;
  - for kind `duplicate`, one of IMPORT-UI-3's choices.

  A decision is made exactly once: a second decision answers `409 already_resolved`, like gates (REVIEW-6). The answer is `202 {job_id}`, and the result arrives as `job.finished` (REVIEW-QUEUE-10).
- IMPORT-UI-9: `POST /api/imports/{id}/lookup {ids?, artist?, album?}` MUST queue a worker job that looks the folder up again and adds the candidates found to the decision. It uses beets' `search_ids`, or `search_artist` and `search_album`. Candidates already listed aren't repeated, and the decision stays `pending`. With no fields it repeats the plain lookup and replaces the candidates. ids: at most 10, each at most 200 characters; free text: at most 200 characters.
- IMPORT-UI-10: With `with.decision_timeout` set (a duration), an undecided decision MUST be decided with `with.on_timeout` (`skip` or `asis`, default `skip`) once the deadline passes, decided by `timeout`, including at server start-up (as REVIEW-9). Without `decision_timeout` a decision waits indefinitely.

### Applying
- IMPORT-UI-11: The decision job MUST run in the profile's worker. If the folder's fingerprint no longer matches, it MUST NOT import. The decision stays `pending` with the error "the folder changed; look it up again" (open question 6). Otherwise:
  - `candidate`: a beets import of that folder alone, with `search_ids` set to the candidate's album id, applying the candidate with that id. If the lookup no longer returns it, the decision stays `pending` with that error.
  - `asis`: a beets import of that folder alone, as-is.
  - `skip`: no import.
  - a `duplicate` choice: a beets import with that `duplicate_action`.

  Imports use the stored `options` and `set` and mark the album with the run (BEETS-IMPORT-5, -13).
- IMPORT-UI-12: After a successful apply the decision MUST become `applied` (or `skipped`) with `decided_by`, `decided_at`, `note` and the album id, and `import.resolved {decision_id, action, album_id?}` MUST be emitted. Then:
  - the lane `pending:<id>` MUST be renamed to the album id, keeping its position and history (LANES-14), or end `skipped` for `skip`;
  - the hold MUST be resolved (`Engine.resolve_hold`);
  - the run MUST be requeued if released (LANES-8) or settled (LANES-7, -9).
- IMPORT-UI-13: If applying fails for any other reason (beets error, lookup error), the decision MUST stay `pending` with the error shown, and the run log gets an error line. Retrying with the same or another action is allowed (open question 5).
- IMPORT-UI-14: Cancelling the run MUST cancel its pending decisions (state `cancelled`, as REVIEW-10); their lanes end `cancelled` (LANES-13).

### UI
- IMPORT-UI-15: The review queue MUST render an import decision with:
  - the folder path and its current items' tags;
  - the candidates, best first, each with distance as a percentage, source, year, label, catalog number and track count, plus a link to the release on its source when beets gives one;
  - for a selected candidate, a track table: current title → candidate title, length, and missing or unmatched tracks;
  - the buttons "Import this match", "Import as-is" and "Skip";
  - "Search by ID" and "Search by artist and album" fields (IMPORT-UI-9);
  - for kind `duplicate`, the existing albums and the four choices.

  An error from IMPORT-UI-11 or -13 MUST be shown on the item with "Look up again". Labels use beets' terms (candidate, as-is, distance, `search_ids`) and link the beets importer docs.
- IMPORT-UI-16: The run page lane table (LANES-15) MUST show a pending lane's hold as "import decision", linked to the queue item.

## Interfaces
```yaml
- id: import
  uses: beets.import
  with:
    path: "{{ run.input }}"
    mode: auto                  # quiet | auto | interactive
    decision_timeout: 7d        # optional (IMPORT-UI-10)
    on_timeout: skip            # skip | asis
```
```python
ImportDecisionKind = Literal["match", "duplicate"]
ImportDecisionState = Literal["pending", "applied", "skipped", "cancelled"]

class ImportCandidate(BaseModel):
    id: str                     # "<data_source>:<album id>", e.g. "MusicBrainz:<mbid>"
    source: str
    artist: str; album: str
    year: int | None; country: str | None; label: str | None
    catalognum: str | None; media: str | None
    tracks: int
    distance: float             # 0 (perfect) .. 1
    penalties: list[str]        # beets' penalty names
    mapping: list[TrackMatchOut]      # item -> track, title change, length
    missing: list[str]; unmatched: list[str]
    url: str | None

class ImportDecision(BaseModel):
    id: str; profile: str; run_id: str; step_id: str
    kind: ImportDecisionKind
    path: str; fingerprint: str
    items: list[FolderItem]                 # path, format, length, current tags
    candidates: list[ImportCandidate]
    duplicates: list[AlbumSnapshot]         # kind duplicate
    options: dict[str, str | bool]; set_fields: dict[str, str]
    state: ImportDecisionState
    error: str | None
    deadline: datetime | None; on_timeout: Literal["skip", "asis"]
    decided_by: str | None; decided_at: datetime | None; note: str | None
    album_id: str | None
```
- Step API, public like `ctx.open_gate` (non-negotiable 7): `ctx.request_import(decision) -> id`. The engine stores the decision through an `ImportDecisionStore` and calls `ctx.hold("pending:<id>", "import", id)`.
- Backend: `LibraryBackend.import_folder(..., mode="auto" | "interactive", decide=...)`. The `decide` callback gets a structured question and returns "defer" (lookup). A new `import_decided(path, action, *, candidate_id=None, duplicate_action=None, run_id, options, set_fields)` does the apply. A `lookup(path, *, ids=(), artist=None, album=None) -> list[ImportCandidate]` serves IMPORT-UI-9. Capability `import_decisions`.
- beets in-process (architecture §8): an `ImportSession` subclass using `choose_match`, `resolve_duplicate`/`get_duplicate_action`, `task.candidates`, `task.rec`, and the `import.search_ids`, `search_artist` and `search_album` config. Contract tests pin them with recorded MusicBrainz responses.
- App DB: `import_decisions` (id, profile, run_id, step_id, kind, state, body JSON, created_at, deadline, decided_at, decided_by, note, error). Worker job kinds `import_decision`, `import_lookup`.
- API: `GET /api/imports/{id}`, `POST /api/imports/{id}/decision`, `POST /api/imports/{id}/lookup`. `QueueItem.kind` gains `import` with an `import_decision` field.
- Events: `import.requested`, `import.resolved` (run stream and `/api/events`).

## Examples
Inbox with three album folders. `mode: auto`, beets finds a strong match for X and Y and
two weak candidates for Z:

1. `import` imports X and Y. Z gets decision D1 with 2 candidates and lane `pending:D1` at `tags`.
2. X and Y run the rest of the workflow and end `done`. The run is `waiting_review`.
3. The user opens D1, searches by ID `MusicBrainz:<mbid>`, gets a third candidate (6 %
   distance) and picks it. The decision job imports Z with that release, renames the lane
   to `album-42`, and the run resumes at `tags` with Z alone.

Reference workflow (`foobar-style.yaml`): checklist lines 1–2 become one step:
```yaml
- id: import
  uses: beets.import
  with: { path: "{{ run.input }}", mode: auto }
```
Strong matches import unattended (line 1); the rest wait as import decisions (line 2).

## Extensibility
- Other backends implement `import_folder` with decisions, `import_decided` and `lookup` behind the `import_decisions` capability. The decision model is backend-neutral (the candidate source is a string).
- beets metadata-source plugins (Discogs, Spotify, …) add candidates without Rifferr changes, because candidates come from beets.
- Fixed: one decision per album folder, the actions listed in IMPORT-UI-8, and candidate ids made of `data_source` and album id.

## Security & safety
- The decision API never takes a path. The folder is fixed when the decision is created, and the apply job checks its fingerprint (IMPORT-UI-11).
- Search input is plain strings with length limits, passed to beets' lookup and never to a shell.
- Mutating routes follow the always-on rules: JSON only, Host/Origin checks, auth (F-SECURITY).
- Lookups contact metadata services only from the worker. Tests use recorded responses, with no network (testing rules).
- Imports are not undone by run undo (C-JOURNAL non-goal). The UI says so on the decision.

## Non-goals
- Per-track (singleton) decisions: loose tracks keep quiet behavior (open question 3).
- Choices that beets plugins add to the terminal prompt (`before_choose_candidate`, e.g. `mbsubmit`): backlog.
- Editing a candidate's tags before import (tag-grid edits after import cover this).
- Several decisions on one screen (queue dashboard, F-REVIEW-QUEUE update).
- Parallel lookups (B-050).

## Open questions
1. Default `mode`? Architecture §2a says `auto`; today the default is `quiet`. Changing it
   changes behavior for workflows that omit `mode` (published schema default). Recommend:
   `auto`, with a release note; both examples set `mode` explicitly.
2. Search by artist and album as well as by ID? Recommend: yes (IMPORT-UI-9). beets
   supports both, and beets-flask offers both.
3. Singletons and loose tracks? Recommend: quiet behavior in M2 (strong match or
   `quiet_fallback`), with a backlog entry for item decisions.
4. Lane state for a skipped album? `rejected` would cancel a run whose only waiting album
   was skipped (LANES-11), and `gone` means something else. Recommend: add `skipped` to
   LANES-2 as a final, non-failure state (amends C-LANES; counts as done for LANES-9).
5. Failed apply: keep the decision pending with the error (retry), or end the lane `failed`?
   Recommend: pending with error. A failed apply usually means a network error or a changed
   folder, and the user can retry or skip.
6. Folder changed between lookup and apply: refuse until it is looked up again, or look it
   up again automatically? Recommend: refuse with "Look up again" (IMPORT-UI-11), so the
   user never applies a match they didn't see.
7. Decision timeout? Recommend: optional `decision_timeout` and `on_timeout: skip | asis`
   (IMPORT-UI-10), off by default, the same as gates.
8. `interactive` vs beets' `timid` option: `interactive` makes every album a decision, which
   is what beets' `import.timid` does at the terminal. Recommend: keep `interactive` as the
   step's mode and also honour `timid: yes` from the user's beets config in `auto`
   (treated as `interactive`).

## Tasks
- [ ] Backend: lookup `ImportSession` with deferral, `import_decided`, `lookup`; capability `import_decisions`; contract tests with recorded MusicBrainz responses (IMPORT-UI-1 to -4, -11).
- [ ] Engine/step: `ctx.request_import`, `ImportDecisionStore`, pending lanes and holds; step params `decision_timeout`, `on_timeout` (IMPORT-UI-5, -6, -10).
- [ ] App DB `import_decisions`; worker jobs `import_decision`, `import_lookup`; lane rename, hold resolve and settle (IMPORT-UI-11 to -14).
- [ ] API routes, queue item kind `import`, events; regenerate schemas and TS client (IMPORT-UI-6 to -9).
- [ ] Review queue UI for import decisions; run page hold link; Playwright: weak match → search by ID → import → lane continues (IMPORT-UI-15, -16).
- [ ] `skipped` lane state if open question 4 is accepted (C-LANES amendment).
- [ ] `foobar-style.yaml` uses `mode: auto`; `docs/coverage.md` line 2; S-BEETS-IMPORT and F-REVIEW-QUEUE notes; mark Implemented.
