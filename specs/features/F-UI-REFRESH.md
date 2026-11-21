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

- UI-REFRESH-18: For the selected candidate, the card MUST show every album field beets compares (artist, album, year, country, label, catalog number, media, mediums, disambiguation, album id, data source) whose value now differs from the candidate's or that carries a penalty: the folder's current value (beets' most common tag across its files), the candidate's value, as a diff (UI-REFRESH-4), with penalized fields marked in `warning`. A penalty such as `country`, `media` or `data_source` is never shown without the values it is about (author, 2026-11-21: "everything that shows up in beets as different should show up").
- UI-REFRESH-19: Each track row MUST show what beets counts as different for that track: a changed track number as a diff, a length penalty as "now → candidate" in `warning`, a track artist column when any track has a `track_artist` penalty, and any other track penalty (for example `track_id`, `medium`) by name in `warning`. A row with a penalty counts as changed even when its title matches (author, 2026-11-21).

Compact cards (extends REVIEW-QUEUE-12, -13)
- UI-REFRESH-10: Every queue item MUST have a one-line header with its selection checkbox, kind, album label, age and its main actions on the right (for example Accept selected / Reject, Continue / Reject, Import this match / Import as-is / Skip). On narrow screens the actions wrap below the header.
- UI-REFRESH-11: The optional note field MUST be hidden behind an "Add note" control until used, and a note typed in it is kept until the item is decided.
- UI-REFRESH-12: A table longer than 8 rows (field changes, tracks, candidates, files) MUST show its first 8 rows and a "Show all N" control. Table rows use compact spacing.

Album-grouped queue (extends REVIEW-QUEUE-11)
- UI-REFRESH-13: Within each run, the queue MUST group items by album lane (C-LANES): one block per lane that holds at least one queue item, labelled with the lane label, in the order the lanes were created. Items in a block are in workflow step order, then oldest first. Items no lane holds go in a last block named "Other". REVIEW-QUEUE-1's API order is unchanged; the grouping is done by the page.
- UI-REFRESH-14: Each album block MUST show a status line from its lane: "waiting on you" while it holds queue items, "running <step id>" while its current step runs, and otherwise its lane state (`done`, `failed`, `skipped`, …), colored by UI-REFRESH-1. The line MUST update from `/api/events` without polling (REVIEW-QUEUE-10).
- UI-REFRESH-15: A run's header MUST show the run's lane counts (LANES-9), for example "3 waiting · 1 running · 5 done", so albums the run is still working on are visible even while they hold nothing.
- UI-REFRESH-16: Grouping MUST keep REVIEW-QUEUE-14 (Accept all / Reject all per run) and REVIEW-QUEUE-16 (`?item=` opens and scrolls to the item, inside its album block) working.

Tests
- UI-REFRESH-17: A Playwright test MUST cover, against the e2e temp profile: a run with two albums that both wait for an import decision. The queue shows two album blocks with "waiting on you"; deciding one album's import changes that block's status line without a reload; candidate badges and penalties are colored by the fixture profile's thresholds.

## Interfaces
Implementation notes (2026-11-21, added while implementing; not new behavior):
- Status tokens are text colors (CSS variables `--rifferr-color-*`, `frontend/src/theme.ts`). Mantine `Alert`, `Button` and row-background tints (tag grid, script preview) keep Mantine colors: a token can't produce their light backgrounds. Badges in a status color are outlined (`TokenBadge`).
- The diff (`frontend/src/lib/diff.ts`) works on grapheme clusters (`Intl.Segmenter`), so accents and emoji are never split. Side-by-side columns (library-move names and folders, import track titles) show each side's own changes (`<Diff side="old|new">`); a ChangeSet's proposed value shows both in one cell.
- Header actions are rendered into the item's header row through a portal (`HeaderActions`), so the card bodies keep their own state and tests find the actions inside the queue item.
- Album blocks (UI-REFRESH-13): a block is shown for every lane that holds a queue item *or is still working* (`active`, `waiting`), so deciding an item turns its block to "running <step>" instead of removing it (UI-REFRESH-14, -17). Finished lanes only count (open question 3). An item held by several lanes (a gate over several albums) goes in a block "Several albums" before "Other".
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
- [x] Playwright test (UI-REFRESH-17); update `review-queue.spec.ts` and `imports.spec.ts`
