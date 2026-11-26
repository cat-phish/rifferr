# F-GENRE-FETCH: Fetch genres for a choice review without AI
Status: Draft
Milestone: M5

## Context
The genre-bucket choice (S-TAGS-CHOOSE, foobar-style `genre-bucket`) ranks buckets by the album's
`subgenres`. An album that came in with no genres (e.g. GBH, *Punk Junkies*, 2026-11-25 test run)
gets no ranked buckets, only custom entry. The author asked for a button that fetches genre
suggestions from MusicBrainz, Discogs and similar sources, with no AI involved (2026-11-25). An
"AI suggest" button is a separate idea (BACKLOG B-098).

beets already fetches genres:
- `lastgenre` (Last.fm tags, mapped through its genre tree);
- the `musicbrainz` plugin, which can take release or release-group genres;
- the `discogs` plugin, which gives genres and styles.

Rifferr's rule is to reuse beets where it already does the job (CLAUDE.md non-negotiable 2). So
this spec asks for a review action that runs those sources again for one album and puts the result
into `rank_by`. It does not add new provider code where a beets plugin covers the source.

## Behavior
- GENRE-FETCH-1: A `tags.choose` step MAY declare `fetch:`, a list of sources (e.g. `lastgenre`,
  `musicbrainz`, `discogs`). Its choice review MUST then show a "Fetch genres" action.
- GENRE-FETCH-2: Fetching MUST run on the album's worker. The sources' values are proposed as a
  ChangeSet to the `rank_by` field (C-CHANGESET), journaled, and the review MUST re-rank its
  options from the new values. Nothing is applied to the target field until the person chooses.
- GENRE-FETCH-3: Each source's values MUST be shown with the source's name, so the person sees
  where a suggestion came from.
- GENRE-FETCH-4: A source that fails (network, no release ID, no token) MUST say so in the review
  and MUST NOT block the others or the decision.
- GENRE-FETCH-5: Fetching MUST be cancellable and emit events (no polling).

## Interfaces
```yaml
- id: genre-bucket
  uses: tags.choose
  with:
    options_from: genres.yaml
    rank_by: subgenres
    custom: [genres]
    learn: true
    fetch: [lastgenre, musicbrainz, discogs]   # proposed
```
- `POST /api/choices/{id}/fetch {sources?}` → a job; `choice.updated` event when re-ranked.

## Examples
GBH, *Punk Junkies*, has no `subgenres`. Pressing "Fetch genres" might return:
- `discogs`: Rock, Punk, Oi
- `musicbrainz`: punk, street punk

After re-ranking, the review offers "Punk" first.

## Extensibility
A source is a plugin entry point (`rifferr.genre_sources`). The built-in sources wrap beets
plugins through the beets backend.

## Security & safety
- Network calls only to the sources' own APIs, with the same HTTP limits as cover providers.
- The Discogs token comes from the keyring or environment, never from logs (non-negotiable 5).

## Non-goals
- AI suggestions (B-098).
- Changing which bucket is chosen: the person still decides.

## Open questions
1. Can beets' `lastgenre`, `musicbrainz` and `discogs` plugins be run for one album without
   re-importing it (e.g. `beet lastgenre -f <query>`, `beet mbsync`)? If not, which sources need
   their own code?
2. Should fetched values replace `subgenres` or be merged into it?
3. Should fetching also run automatically when an album arrives with no `subgenres`, before the
   review opens?
4. Which Discogs data counts: genres, styles, or both?

## Tasks
- [ ] Answer the open questions; author approves
- [ ] Backend: sources, fetch job, re-rank; tests with recorded HTTP fixtures
- [ ] UI: "Fetch genres" action and per-source values; Playwright test
