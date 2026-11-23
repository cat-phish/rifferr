# F-UI-REFRESH: Status colors, compact review cards and album-grouped queue
Status: Implemented
Milestone: M2

## Context
In real test runs (2026-11-21) the review queue was hard to follow:
- Changes barely stand out. Changed values are orange (ChangeSets) or blue (import track titles), and several tables (gate changes, library moves) have no color at all.
- Candidate distance is an uncolored badge, and beets' penalties are sent by the API (`ImportCandidate.penalties`) but never shown.
- Each card is tall and narrow: stacked tables, an always-open note field, and the folder's file list above the candidates. Wide screens are mostly empty.
- Within a run, items are a flat list, oldest first (REVIEW-QUEUE-1). One album's items are spread out, and nothing shows what the run is doing for an album between two items.

beets' importer is the model, because users of Rifferr already read it. `beet import` colors a candidate's similarity green, yellow or red against the user's `match.strong_rec_thresh` and `match.medium_rec_thresh`. It lists penalties in yellow, shows changed values with removed text red and added text green (`ui.colors`: `text_success`, `text_warning`, `text_error`, `text_diff_removed`, `text_diff_added`), and shows missing and unmatched tracks in yellow.

This spec adds behavior to F-UI-SHELL (theme), F-REVIEW-QUEUE (REVIEW-QUEUE-11, -12, -13) and F-IMPORT-UI (IMPORT-UI-15). It changes how they look and how the queue is grouped, not what they do. API routes, decisions and events are unchanged except where the Interfaces section says so.

## Behavior
Colors
- UI-REFRESH-1: The frontend MUST define its status colors once, as named tokens in the Mantine theme, for light and dark schemes (UI-SHELL-12): `success` (green: a strong match, an unchanged or applied value), `warning` (yellow: a medium match, a penalty, a missing or unmatched track, a conflict), `danger` (red: a weak match, an error), `changed` (red: a value the step will change), `diffRemoved` (red) and `diffAdded` (green). Components MUST use the tokens, not Mantine color names such as `c="orange"`.
- UI-REFRESH-2: Each token's text MUST reach WCAG AA contrast (4.5:1) on the card background in both schemes. A test checks the computed pairs.
- UI-REFRESH-3: Color MUST never be the only cue. A changed value also shows its old value struck through, a match badge also shows its percentage, and warnings also carry a word or icon.
- UI-REFRESH-4: Every place that shows an old and a new value (ChangeSet fields, gate changes, import track titles, library-move paths) MUST render them as a diff: unchanged text plain, removed parts in `diffRemoved` (struck through) and added parts in `diffAdded`. The diff works on characters for short values and on words when the values share fewer than half their characters, as beets does. Values are still text, never HTML (REVIEW-QUEUE-17).
- UI-REFRESH-5: An unchanged value next to changed ones MUST be shown dimmed, so the eye lands on changes.

Import decisions (extends IMPORT-UI-15)
- UI-REFRESH-6: Each candidate's match MUST be shown as beets shows it: similarity = (1 − distance) as a percentage, colored `success` when distance ≤ the profile's `match.strong_rec_thresh`, `warning` when ≤ `match.medium_rec_thresh`, else `danger`. The thresholds come from the backend (Interfaces), never from frontend constants. A backend that sends no thresholds gets an uncolored badge.
- UI-REFRESH-7: Each candidate MUST list its penalties (`ImportCandidate.penalties`, with underscores shown as spaces) in `warning`, as `beet import` does.
- UI-REFRESH-8: The track table MUST show "Missing tracks" and "Unmatched files" in `warning` and changed titles through the diff (UI-REFRESH-4). Matching titles are dimmed (UI-REFRESH-5).
- UI-REFRESH-9: On screens at least the `md` breakpoint wide, an import card MUST put the candidate list and the selected candidate's track table side by side. Narrower screens stack them. The folder's current items (IMPORT-UI-15, first bullet) MUST be collapsed by default behind "Show the folder's N files".

- UI-REFRESH-18: For the selected candidate, the card MUST show every album field beets compares (artist, album, year, country, label, catalog number, media, mediums, disambiguation, album id, data source) whose value now differs from the candidate's or that carries a penalty: the folder's current value (beets' most common tag across its files), the candidate's value, as a diff (UI-REFRESH-4), with penalized fields marked in `warning`. A penalty such as `country`, `media` or `data_source` is never shown without the values it is about (author, 2026-11-21: "everything that shows up in beets as different should show up"). Above that comparison the card MUST repeat the selected candidate's similarity badge and name, so the match is seen at a glance (author, 2026-11-22).
- UI-REFRESH-19: Each track row MUST show what beets counts as different for that track: a changed track number as a diff, a length penalty as "now → candidate" in `warning`, a track artist column when any track has a `track_artist` penalty, and any other track penalty (for example `track_id`, `medium`) by name in `warning`. A row with a penalty counts as changed even when its title matches (author, 2026-11-21).

Compact cards (extends REVIEW-QUEUE-12, -13)
- UI-REFRESH-10: Every queue item MUST have a one-line header with its selection checkbox, kind, album label, age and its main actions on the right (for example Accept selected / Reject, Continue / Reject, Import this match / Import as-is / Skip). On narrow screens the actions wrap below the header.
- UI-REFRESH-11: The optional note field MUST be hidden behind an "Add note" control until used, and a note typed in it is kept until the item is decided.
- UI-REFRESH-12: A table longer than 8 rows (field changes, tracks, candidates, files) MUST show its first 8 rows and a "Show all N" control. Table rows use compact spacing.

Album-grouped queue (extends REVIEW-QUEUE-11)
- UI-REFRESH-13: The queue MUST be grouped into one section per workflow step (REVIEW-QUEUE-11), in the workflow's step order, so different stages (an import decision, a library move) never mix. Within a section items are oldest first: a new item always joins the bottom (author, 2026-11-21: library moves jumped to the top because runs were newest first, and one run per album made that the norm). REVIEW-QUEUE-1's API order is unchanged; the grouping is done by the page. Replaces the per-run album blocks. Each section is a tab (author, 2026-11-21), labeled with its step id (and workflow when several wait) and its item count, in that order, with no "All" tab. The tabs are permanent (author, 2026-11-22: tabs came and went as runs moved on): one per step of the shown workflow (UI-REFRESH-22) that can wait for review, whether or not anything waits there, in step order, with its count; a tab at 0 is dimmed and says "Nothing waiting at this step". A step can wait when its type's `waits` is `always`, or `apply` and the step's `apply` is `review` (or the type has `force_review`); the workflow detail lists its steps, calls expanded, with `may_wait`. Items of any other step get that step's tab while they wait, and items with no step an "Other" tab. The open tab is kept in the URL (`?stage=<step>`); by default the first tab with items opens, and a `?item=` deep link opens its item's tab. The page never switches tab on its own.
- UI-REFRESH-14: Each item's header MUST show its album (the label of the one lane that holds it, C-LANES) and a link to its run. There is no status line and no list of albums still working on this page; the run page shows those (author, 2026-11-21: "I just want it to disappear").
- UI-REFRESH-15: Withdrawn (author, 2026-11-21): run lane counts are no longer shown on the review page, since it no longer groups by run. The run page keeps them (LANES-9).
- UI-REFRESH-16: Sections MUST keep REVIEW-QUEUE-14 (Accept all / Reject all, now per section) and REVIEW-QUEUE-16 (`?item=` opens and scrolls to the item; `?run=` shows one run's items) working.

- UI-REFRESH-20: Once a decision is sent (the API accepted it), its item MUST leave the list at once, with nothing in its place, so the next item is right there. The item MUST come back when it changes (an error, a conflict) or when the job carrying out the decision fails (`job.finished` with that job id and a state other than `succeeded`) while the item still waits. A succeeded job does not bring it back: the run may still be carrying the decision out (a library move applies when the run resumes), and the item leaves once it has (author, 2026-11-21: a moved album flashed back as new items arrived). A decision the API refuses keeps the item where it is, with the error (author, 2026-11-21: clicking import left the item "waiting on you" for a long time). While its decision job is queued or running the server marks it `busy`, so it stays gone after a reload or refetch (REVIEW-QUEUE-19; author, 2026-11-22).
- UI-REFRESH-21: (Added by the author, 2026-11-21.) A library move (S-LIBRARY-MOVE LIBRARY-MOVE-16) MUST show as one line per album: the old and new folder and its actions. The file list, the date note and the note field sit behind "Show the N files". When every proposed item of a stage is a file move, the stage's accept-all button MUST read "Move all N" ("Copy all N" when any copies), and its confirmation counts albums. Conflicts stay separate items (LIBRARY-MOVE-5a).
- UI-REFRESH-22: (Added by the author, 2026-11-22.) The review page MUST show one workflow at a time (`?workflow=<name>`): a deep-linked item's, else the URL's, else the first with items. When more than one workflow is listed, the Review nav entry MUST list each one under it with its count of waiting items, even at 0. Listed are the profile's workflows with a step that can wait (UI-REFRESH-13) and any workflow with waiting items. Author, 2026-11-22: the list under Review MUST fold away with its own toggle (remembered in the browser) without leaving the page, and the count next to Review MUST stay upright. Plain `/review` (no `workflow`, `item` or `run`) MUST be an overview when more than one workflow is listed: one card per workflow with its waiting count, the age of its oldest item, and each stage tab's count linking to that tab. With one workflow it opens that workflow at once. (Amended by the author, 2026-11-22, UI-REFRESH-23: the page is now each workflow's Review tab, the nav entry is "Workflows" and lists every workflow.)
- UI-REFRESH-23: (Added by the author, 2026-11-22.) The Review page MUST become the Workflows hub. `/workflows` lists every workflow of the profile as a card with its waiting count by stage and links to its page, with an "Edit workflows" button leading to the workflow list (`/workflow-files`: trust, vars, start). The nav entry "Workflows" carries the total waiting count and folds out every workflow with its count (UI-REFRESH-22). `/workflows/<name>` has the tabs Review (the stage tabs of UI-REFRESH-13; default), Runs (that workflow's runs, `GET /api/runs?workflow=`) and Details (steps, triggers, trust and safety report, start), kept in `?tab=`; items of no known workflow live at `/workflows/~other` with only Review. An old `/review` link (`?item=`, `?run=`, `?workflow=`, `?stage=`) MUST go on to that workflow's page with the same parameters; plain `/review` goes to the hub, or straight to the only workflow.
- UI-REFRESH-24: (Added by the author, 2026-11-22: albums were invisible while they ran through steps that don't wait.) A workflow's Review tab MUST become **Pipeline**, still the default tab (`?tab=review` still opens it). It MUST have a stage tab for every step of the workflow (calls expanded), in step order, labeled with the step id, its count of waiting items (as UI-REFRESH-13) and, dimmed, its count of albums at that step that aren't waiting on review. Waiting items MUST always come first: whenever anything waits, a "Waiting for review" bar above the stage tabs shows one chip per stage with its count, each opening that tab, and it updates live, so a new item at another stage is seen without switching tabs (the page still never switches tab on its own). Inside a stage tab the waiting items come first (UI-REFRESH-13 order), then "At this step": each live album whose lane `position` is that step and that isn't waiting on review, with its label, lane state, a link to its run and how long it has been at the step; read-only. Live means a lane in no final state of a run that is queued, running or waiting. A queued or running run with no lanes yet shows in the first tab as "Run starting" with its input's folder name; lanes past the last step but not final show in a trailing "Finishing" tab only while there are some. `GET /api/pipeline?profile=&workflow=` returns the live lanes and starting runs. By default the first tab with waiting items opens, else the first with albums, else the first.

Tests
- UI-REFRESH-17: A Playwright test MUST cover, against the e2e temp profile: a run with two albums that both wait for an import decision. Both items are in the "import" section with their album labels; candidate badges and penalties are colored by the fixture profile's thresholds; importing one album removes its item at once while the other still waits.

## Interfaces
Implementation notes (2026-11-21, added while implementing; not new behavior):
- Status tokens are text colors (CSS variables `--rifferr-color-*`, `frontend/src/theme.ts`). Mantine `Alert`, `Button` and row-background tints (tag grid, script preview) keep Mantine colors: a token can't produce their light backgrounds. Badges in a status color are outlined (`TokenBadge`).
- The diff (`frontend/src/lib/diff.ts`) works on grapheme clusters (`Intl.Segmenter`), so accents and emoji are never split. Side-by-side columns (library-move names and folders, import track titles) show each side's own changes (`<Diff side="old|new">`); a ChangeSet's proposed value shows both in one cell.
- Header actions are rendered into the item's header row through a portal (`HeaderActions`), so the card bodies keep their own state and tests find the actions inside the queue item.
- Step sections (UI-REFRESH-13, -14, -20; amended 2026-11-21): sections are keyed by workflow and step id and ordered by the step list of one run per workflow (the cached run query); unknown steps go last. The workflow name prefixes the label only when several workflows wait. Decided items are kept per page in memory with their content at decision time and the decision's job id; a changed item, or a refetch after that job's `job.finished`, brings it back.
- The page reads lanes from `GET /api/runs/{id}` with the run page's query key. `run.step.started` and `run.step.finished` (not `run.step.log`) now go on `/api/events` (`api/state.py` `STEP_EVENTS`), so a lane's step changes reach the queue. API-9's test allows them.
- UI-REFRESH-18/-19 data: `ImportCandidate.fields` (`FieldComparison`: field, current, candidate, penalized) and per-track `penalties`, `track_before`, `length_before`, `artist_before`, `artist_after` on `TrackMatch`. The beets backend takes the current values from `beets.util.get_most_common_tags` over the folder's files, the same values beets' `distance()` compares, and the penalties from `match.distance` and `match.distance.tracks`.
- `match_thresholds` lives on `ImportDecision` and is refreshed on every lookup, like `lookup_problem` (IMPORT-UI-20).

- Theme: `frontend/src/theme.ts`, a `createTheme` with the tokens of UI-REFRESH-1 as CSS variables (`--rifferr-color-success`, …) for both schemes, passed to `MantineProvider` (`frontend/src/providers.tsx`).
- Shared components: `<Diff old new />`, `<MatchBadge distance thresholds />`, `<Penalties names />`, `<Collapsible rows limit={8} />`. The diff itself is `frontend/src/lib/diff.ts`, with no new dependency.
- API, import decisions: the queue item for an import decision gains

  ```json
  "match_thresholds": {"strong": 0.04, "medium": 0.25}
  ```

  The field is `null` when the backend has no such notion. `rifferr.backends.beets` reads it from the profile's beets config (`match.strong_rec_thresh`, `match.medium_rec_thresh`), and FakeBackend returns fixed values. It goes through the `LibraryBackend` protocol (an optional `match_thresholds()` method with default `None`), so the API server never touches beets. A contract test pins both beets config keys and their defaults.
- API, review queue: no new route. The page gets lanes from `GET /api/runs/{id}` (LANES-15) for each run in the queue, and maps items to lanes by `LaneOut.holds[].ref`. `run.step.*` and lane events invalidate that run's query.
- Regenerate `schemas/` and `frontend/src/api/schema.d.ts` after the model change.

## Examples
With thresholds strong 0.04 and medium 0.25:

| distance | shown | color |
|---|---|---|
| 0.021 | 97.9 % | success |
| 0.130 | 87.0 % | warning |
| 0.400 | 60.0 % | danger |

Diff of a track title `Intro (Remastered 2011)` → `Intro`: `Intro` plain, ` (Remastered 2011)` red and struck through.

Album block:

```
▾ Pink Floyd - Meddle                      waiting on you
   [ ] Import decision · 2 min    [Import this match] [Import as-is] [Skip]
   candidates (left) | track table (right)
▾ Can - Tago Mago                          running covers.search
```

## Extensibility
- Step and plugin authors don't need to do anything: the colors come from the shared components.
- The token values are fixed in the theme for now. User-configurable colors (as with beets `ui.colors`) is a non-goal here.
- Other backends can send their own `match_thresholds`, or none.

## Security & safety
- Diffs and labels render as text nodes only (REVIEW-QUEUE-17). No `dangerouslySetInnerHTML`.
- There are no new mutating endpoints. The one new response field is read-only.

## Non-goals
- User-chosen color themes, or reading beets' `ui.colors`.
- Changing decision semantics, API order (REVIEW-QUEUE-1) or events.
- Restyling the tag grid, scripts and workflow pages beyond moving their existing colors to the tokens.
- Why some items seem to reappear at the bottom of the queue. The author set this aside on 2026-11-21 (B-076).
- A music-app-style viewer for albums waiting before the library move (staging): large cover art, an album header and track tags. That will be its own spec (F-ALBUM-REVIEW), built on this spec's tokens and diff.

## Open questions
1. Similarity or distance on the badge? → similarity, labelled "similarity", as `beet import` (author, 2026-11-21). IMPORT-UI-15's "distance as a percentage" is shown this way.
2. Fold limit for UI-REFRESH-12? → 8 rows (author, 2026-11-21).
3. Finished lanes: collapsed blocks or counts only? → counts only (UI-REFRESH-15), no block (author, 2026-11-21).

## Tasks
- [x] Theme tokens and contrast test (UI-REFRESH-1, -2)
- [x] `lib/diff.ts` and `<Diff>`; replace ad-hoc colors (UI-REFRESH-3, -4, -5)
- [x] `match_thresholds` through backend protocol, beets backend, FakeBackend, contract test, schemas, TS client (UI-REFRESH-6)
- [x] Import card: badge, penalties, track table, two-column layout, collapsed files (UI-REFRESH-6 .. -9)
- [x] Compact card header, note toggle, row folding (UI-REFRESH-10 .. -12)
- [x] Album blocks, status line, run lane counts (UI-REFRESH-13 .. -16)
- [x] Album field comparisons and track penalties from beets; contract tests (UI-REFRESH-18, -19)
- [x] First-come order, working list, decided items leave at once (UI-REFRESH-13, -14, -20)
- [x] Sections per workflow step instead of runs; no working list; section bulk (UI-REFRESH-13 .. -17, -20; REVIEW-QUEUE-11, -14)
- [x] Playwright test (UI-REFRESH-17); update `review-queue.spec.ts` and `imports.spec.ts`
- [x] Stage tabs (UI-REFRESH-13); compact library moves and "Move all N" (UI-REFRESH-21)
- [x] Permanent stage tabs per step that can wait; workflows under Review in the nav (UI-REFRESH-13, -22)
- [x] Review overview page, folding workflow list, chosen match's similarity above the comparison (UI-REFRESH-18, -22)
- [x] Workflows hub with per-workflow Review / Runs / Details tabs; `/review` links go on there (UI-REFRESH-23)
- [x] Pipeline tab: every step a stage tab, albums at each step, waiting-for-review bar; `GET /api/pipeline` (UI-REFRESH-24)
