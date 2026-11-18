# S-COVERS-SEARCH: `covers.search` step and cover provider protocol
Status: Implemented
Milestone: M3

## Context
Architecture §6 describes multi-provider cover search: a provider protocol that streams
candidates, ranking by `provider_weight × match_confidence × size_score` with hard filters,
per-profile weights and provider order, a per-query cache, and output settings. M3's done-when
is "the user can pick art from at least three providers with weights". The reference checklist
(`docs/original-checklist.md`, coverage row 6) starts with "first pass through entire album
list checking for album art", and also uses cover search as a way to look an album up when a
date is missing.

This spec covers the backend half: the provider protocol, the first providers, ranking, the
cache, the `covers.search` step, the review item it creates when a human must choose, the API
that search and choice go through, and how a chosen cover is written to the library. The
picker screen (streaming grid, editable query, toggles and sliders, preview) is F-COVER-PICKER.

Writing a cover changes the library, so it must follow non-negotiable 3 (all modifications are
ChangeSets, journaled before anything changes). Today a ChangeSet holds only field changes
(C-CHANGESET), so this spec adds a cover change to it (COVERS-SEARCH-14 to -16, Open question 1).

Like `fetchart`, beets can already fetch covers, but its sources, ranking and choice are fixed
inside the plugin and it doesn't stream candidates to a UI or wait for a human in a review
queue. Rifferr uses beets for what beets does well here: `Album.set_art` places the file and
sets `artpath`, and beets' `art_filename` setting names it. Users who prefer `fetchart` can
still run it as a `beets.command` step.

## Behavior
### Providers
- COVERS-SEARCH-1: A cover provider MUST implement the `CoverProvider` protocol (Interfaces):
  a `name`, the `hosts` it may contact, whether it `needs` a secret, and
  `search(query, settings, http) -> AsyncIterator[CoverCandidate]`. Providers MUST be loaded
  through the `rifferr.covers` entry-point group, built-ins included (non-negotiable 7). A
  provider MUST make HTTP requests only through the `http` client it is given.
- COVERS-SEARCH-2: A `CoverQuery` MUST hold `artist`, `album`, optional `year`, `catalognum`,
  `barcode`, `mb_albumid`, `mb_releasegroupid`, and `folder` (the album's folder, for local
  files). The step MUST build it from the album snapshot's fields named in the step's `query`
  map (default: beets field names `albumartist`, `album`, `year`, `catalognum`, `barcode`,
  `mb_albumid`, `mb_releasegroupid`), so a profile with other field names can map them
  (non-negotiable 1). Each map value is a `{{ }}` template.
- COVERS-SEARCH-3: A `CoverCandidate` MUST hold `provider`, `image_url` (or `path` for local
  files), optional `thumb_url`, `width`, `height` (`null` when the provider doesn't say),
  `match_confidence` (0–1), optional `source_url` (the provider's page for the release) and
  `title` (what the provider matched, e.g. "Artist – Album (2004)"). Its `id` MUST be a stable
  hash of `provider` and `image_url`/`path`.
- COVERS-SEARCH-4: The built-in providers for M3 MUST be:
  - `coverartarchive`: Cover Art Archive front images by `mb_albumid`, then by
    `mb_releasegroupid`; no result without either. `match_confidence` 1.0.
  - `itunes`: the iTunes Search API, entity `album`.
  - `deezer`: the Deezer public search API, type `album`.
  - `local`: image files in `query.folder` whose names match the provider setting `patterns`
    (default `["*.jpg", "*.jpeg", "*.png"]`), sizes read from the file header.
  For `itunes` and `deezer`, `match_confidence` MUST come from `match_score` (COVERS-SEARCH-8)
  of the query against the provider's artist and album strings. A provider that needs a secret
  (`needs: true`) MUST be skipped, with a run-log note, when its secret isn't set.
- COVERS-SEARCH-5: The `http` client MUST: allow only `https` URLs to the provider's `hosts`
  (redirects included; CAA's redirect to `archive.org` hosts is listed); send a
  `User-Agent` of `Rifferr/<version> (+<project url>)`, plus the profile's `covers.contact`
  when set (no contact is built in); time out each request after `covers.timeout` (default
  `15s`); and refuse a response body over `covers.max_bytes` (default 25 MB). A provider error
  or timeout MUST end only that provider's results, logged with the provider name, and the
  search MUST go on with the others. `covers.timeout` bounds a whole `CoverHttp.get` (all hops and the body), not each phase; slow links with big covers may need a larger value.
- COVERS-SEARCH-6: Providers MUST run concurrently, and candidates MUST be emitted as each
  provider yields them (the UI never polls, architecture tenets). A search MUST be cancellable.

### Ranking and filters
- COVERS-SEARCH-7: Each candidate's `score` MUST be
  `weight(provider) × match_confidence × size_score`, where `size_score` is
  `min(1, min(width, height) / covers.target_size)` (default `target_size` 1000), or
  `covers.unknown_size_score` (default 0.5) when the size is unknown. Ties MUST break by the
  provider order in the profile, then by larger area.
- COVERS-SEARCH-8: `match_score(query, artist, album)` MUST be a pure function in `core`
  returning 0–1 from normalised string similarity (case, punctuation, diacritics and a leading
  "The" ignored for comparison only; no list of words or names is built in beyond what the
  profile's `covers.normalize` adds). A year that differs by more than one MUST multiply the
  score by `covers.year_penalty` (default 0.8) when both years are known.
- COVERS-SEARCH-9: Hard filters MUST drop a candidate before ranking when its known size is
  under `covers.min_size` (default 500 px on the short side), or its aspect ratio differs from
  1 by more than `covers.max_aspect_error` (default 0.1). Dropped candidates MUST be counted per
  provider and reason, and the counts shown with the results. Unknown sizes pass the filters;
  when the image is downloaded (COVERS-SEARCH-13) its real size MUST be checked again.
- COVERS-SEARCH-10: Provider order, `enabled` and `weight` (0–10, default 1) MUST live in the
  profile's `covers.providers` list; a step's `providers` option MAY override them for that
  step only. Ranking MUST be a pure, tested function independent of the providers.

### Cache
- COVERS-SEARCH-11: Search results MUST be cached per provider and normalised query in the app
  data folder for `covers.cache_ttl` (default `7d`). Thumbnails and downloaded images MUST be
  cached by URL, and stored images by their sha256. A cached result MUST be used without a
  request until it expires; `refresh: true` on a search (API or step) MUST bypass it. Cache
  size MUST be capped by `covers.cache_max` (default 500 MB), oldest used first out.

### The `covers.search` step
- COVERS-SEARCH-12: `covers.search` MUST run once per album. With `only_missing: true`
  (default) it MUST skip albums that already have art: an `artpath` that exists, or a
  `local`-provider match when `local` is enabled and `local_counts: true` (default). Skips
  MUST be logged per album.
- COVERS-SEARCH-13: With `choose: auto` (default), the step MUST take the top-ranked candidate
  if its score is at least `min_score` (default 0.6) and download it; the bytes MUST be an
  image (JPEG or PNG by magic bytes) whose real size passes the filters, else the next
  candidate is tried, up to `max_tries` (default 3). When no candidate qualifies, the album MUST
  follow `on_none` (`review` default, `skip`, or `fail`). With `choose: review`, every album
  MUST go to review.
- COVERS-SEARCH-14: Going to review MUST create a `cover` review item for that album holding
  the query and the ranked candidates, and pause only that album's lane (C-LANES,
  non-negotiable 6); the `review.needed` notification rule covers it (NOTIFY-8). The
  candidates MUST be stored with the item, so the review shows them after a restart without
  searching again.
- COVERS-SEARCH-15: A chosen cover (by the step or by a human) MUST be proposed as a ChangeSet
  whose album target carries an `art` change: the image's `sha256`, `width`, `height`,
  `mime`, `provider`, `source_url`, and the album's current art `sha256` as the expected old
  value (`null` when it has none). It follows the step's `apply` policy like any ChangeSet
  (`auto` default for this step; `review` puts the ChangeSet in the review queue). The image
  bytes MUST already be in the cache's sha256 store when the ChangeSet is proposed.

### Applying and undoing a cover
- COVERS-SEARCH-16: `LibraryBackend` MUST gain the capability `art: bool`. Applying an `art`
  change MUST: journal first (JOURNAL-1), copying the album's current art file, if any, into
  the journal's art store by sha256; then call `set_art(album_id, image_path)`, which in
  `BeetsBackend` is beets' `Album.set_art` (file placed in the album folder, named by beets'
  `art_filename`, `artpath` set) followed by `store()`. A current art sha256 that differs from
  the expected old value MUST be a conflict (CHANGESET-8). Backends without `art` MUST reject
  the change with a clear error.
- COVERS-SEARCH-17: Undo of an `art` change (JOURNAL-4) MUST restore the old file from the
  journal's art store, or remove the art (`artpath` cleared, file deleted only if it is the one
  this change wrote, by sha256) when there was none. It MUST conflict, without changing
  anything, when the current art isn't the one the change wrote.
- COVERS-SEARCH-18: `FakeBackend` MUST implement `set_art` and art in snapshots in memory with
  the same semantics; the backend contract suite MUST cover both.

### API
- COVERS-SEARCH-19: `POST /api/covers/search {profile, query, providers?, refresh?}` MUST start
  a search and answer `202 {search_id}`; candidates, per-provider status (`searching`, `done`,
  `failed`, `skipped`) and filter counts MUST stream as `covers.candidate`,
  `covers.provider` and `covers.done` events on the profile's stream. `DELETE
  /api/covers/search/{id}` MUST cancel it. Searches run in the server, not the beets worker:
  they need no library access.
- COVERS-SEARCH-20: `GET /api/covers/image/{sha256|candidate id}?thumb=1` MUST serve cached
  images only (never fetch an arbitrary URL for the client), with the right `Content-Type`
  and `Content-Security-Policy: default-src 'none'`. The route takes optional `review_id` and `album_id`; a local (path) candidate MUST be served only when its parent equals that review's or album's folder, else 400. The UI passes them.
- COVERS-SEARCH-21: `POST /api/reviews/covers/{item_id}/decision {decision: choose|skip|fail,
  candidate_id?, query?}` MUST resolve a `cover` review item: `choose` downloads the candidate
  if needed (COVERS-SEARCH-13 checks apply) and proposes its ChangeSet (COVERS-SEARCH-15),
  `skip` resumes the lane without a cover, `fail` fails that album. A candidate from a new
  search with an edited `query` MUST be accepted. The review list (REVIEW-QUEUE-1) MUST include
  `cover` items.
- COVERS-SEARCH-22: Snapshots (C-BACKEND) MUST expose an album's art as `art: {sha256, width,
  height, mime} | null`, and `GET /api/albums/{id}/art` MUST serve the current art file of an
  album through the read-only worker.
- COVERS-SEARCH-23: The album art route MUST answer 502 `bad_image` for a file larger than 4 x `covers.max_bytes`, and MUST read it bounded.
- COVERS-SEARCH-24: CoverArtArchive ids that are not UUID-shaped MUST be skipped (lower-cased otherwise).
- COVERS-SEARCH-25: `CoverCandidate.source_url` MUST be null unless it is an http(s) URL; the UI re-checks.

## Interfaces
```yaml
# profile.yaml
covers:
  contact: null                 # added to the User-Agent, e.g. a URL or address
  timeout: 15s
  max_bytes: 25MB
  target_size: 1000
  min_size: 500
  max_aspect_error: 0.1
  unknown_size_score: 0.5
  year_penalty: 0.8
  cache_ttl: 7d
  cache_max: 500MB
  normalize: []                 # extra regex replacements for match_score
  providers:                    # order = tie-break order
    - {name: coverartarchive, weight: 3}
    - {name: local, weight: 2, settings: {patterns: ["cover.*", "folder.*", "*.jpg"]}}
    - {name: itunes, weight: 1}
    - {name: deezer, weight: 1, enabled: false}
```
```yaml
# workflow step
- id: art
  uses: covers.search
  with:
    only_missing: true
    choose: auto                # auto | review
    min_score: 0.6
    max_tries: 3
    on_none: review             # review | skip | fail
    apply: auto                 # ChangeSet apply policy
    providers: [coverartarchive, itunes]   # optional override
    query: {artist: "{{ album.albumartist }}"}
```
```python
class CoverQuery(BaseModel, frozen=True):
    artist: str; album: str; year: int | None = None
    catalognum: str | None = None; barcode: str | None = None
    mb_albumid: str | None = None; mb_releasegroupid: str | None = None
    folder: str | None = None

class CoverCandidate(BaseModel, frozen=True):
    id: str; provider: str; title: str | None = None
    image_url: str | None = None; path: str | None = None; thumb_url: str | None = None
    width: int | None = None; height: int | None = None
    match_confidence: float; source_url: str | None = None

class RankedCandidate(CoverCandidate):
    score: float

class CoverProvider(Protocol):
    name: str; hosts: frozenset[str]; needs: bool
    def search(self, query: CoverQuery, settings: Mapping[str, object],
               http: CoverHttp) -> AsyncIterator[CoverCandidate]: ...

class ArtChange(BaseModel, frozen=True):        # on an album TargetChange
    sha256: str; width: int; height: int; mime: Literal["image/jpeg", "image/png"]
    provider: str; source_url: str | None = None; old_sha256: str | None
```
- `TargetChange` gains `art: ArtChange | None`; `changes` may be empty when `art` is set. The
  ChangeSet proposal schema change is additive.
- `LibraryCapabilities.art`; `LibraryBackend.set_art(album_id, path) -> ArtInfo` and
  `clear_art(album_id, *, delete: bool)`.
- App DB (migration): `cover_reviews` (id, run_id, lane, album_id, query JSON, candidates
  JSON, state, decided_by); journal art store under the app data folder.
- Events: `covers.candidate`, `covers.provider`, `covers.done` (search); review item kind
  `cover`.

### Implementation details
Where the build differs from the text above, and why (2026-11-14):
- **The cover is a field change, not `TargetChange.art`.** An album's cover is the album field
  `art_sha256` (`core.covers.ART_FIELD`): the sha256 of its art file, computed by the backend
  from `artpath`. A cover ChangeSet sets it (or deletes it to remove the art). This keeps
  Open question 1's intent, since review, conflicts, the journal, undo and import undo need no
  changes, and the published proposal schema doesn't change at all. `ArtChange` was not
  built. The image's size, type and provider are in the ChangeSet's `rationale`. Snapshots
  show `art_sha256` (and beets' own `artpath`) instead of an `art` object (COVERS-SEARCH-22).
- **Nothing is ever lost.** Before beets replaces or removes an art file, or overwrites an
  image already at the art destination, the backend copies it into the art store (by
  sha256, never evicted). Undo can always restore it, and removing art deletes the file only
  once a copy is stored. A cover is refused when the album's folder doesn't exist.
- **Entry points:** providers register under `rifferr.cover_providers`, the group name
  `rifferr.plugins` already published, not `rifferr.covers`.
- **Query fields are backend templates** (`$albumartist`, beets syntax), evaluated per album,
  not `{{ }}`. Workflow interpolation runs once per step, not per album.
- **`apply` defaults to `auto` through a new optional step-type attribute,
  `default_apply`.** It applies when the workflow doesn't set `apply`. Called workflows keep
  this.
- **A person's choice applies at once.** Choosing in the review queue is already the review,
  so the `choose` job applies the ChangeSet without a second review. Its `old` is the
  album's art when applied.
- **Routes:**
  - Decisions are `POST /api/covers/reviews/{id}/decision`, which answers 202 for every
    decision, like cleanups.
  - `GET /api/covers/reviews/{id}` returns one review.
  - `DELETE /api/covers/search/{id}` answers 200 with the search id.
  - Searches from the API ignore `query.folder`: the `local` provider only looks in the
    folder of the review named by `review_id`, and a chosen local image must be in that
    folder.
  - Image routes may set their own stricter CSP. The security middleware keeps it and adds
    `frame-ancestors 'none'`, so this only tightens the rule.
- **Probing:** `choose: auto` downloads candidates whose size the provider didn't give
  (up to `max_tries`) and ranks them again at their real size. iTunes and Cover Art Archive
  give no sizes.
- **Secrets:** a provider with `needs: true` names its secret as `settings.secret`. The
  value reaches it as `settings["token"]`, and the profile's matching rules arrive under
  `settings["_match"]` (`covers.base.confidence`).
- **UI:** a cover review shows in the queue as a "Cover choice" card: the stored
  candidates, "Use this cover", "No cover" and "Fail this album". The full picker
  (streaming search, editable query, weights, preview) is F-COVER-PICKER, which also brings
  the Playwright test.
- Provider replies in `backend/tests/covers/replies/` are hand-written from the documented
  API shapes, since tests have no network.

## Examples
Query `{artist: "Boards of Canada", album: "Geogaddi", year: 2002}`, weights CAA 3, iTunes 1:
- CAA 1500×1500, confidence 1.0: score `3 × 1.0 × 1.0 = 3.0`.
- iTunes 600×600, "Boards of Canada – Geogaddi", confidence 1.0: `1 × 1.0 × 0.6 = 0.6`.
- iTunes 1200×800: dropped (aspect error 0.33), counted as `aspect`.
Auto mode takes the CAA image. Without an MBID, CAA yields nothing; iTunes's 0.6 meets
`min_score` and is taken.

## Extensibility
- New providers through `rifferr.covers` entry points (Discogs, fanart.tv, Last.fm, a search
  engine); they declare hosts and secrets and get the same client, cache and ranking.
- Everything numeric is a profile setting; field mapping is per step.
- Fixed: the score formula's shape, the hard filters, the host allowlist, the art ChangeSet.

## Security & safety
- Outbound requests only to each provider's declared hosts, `https` only, with size and time
  caps. The server never proxies a client-supplied URL (COVERS-SEARCH-20).
- Images are checked by magic bytes, served with a no-script CSP, and never executed or
  parsed beyond their header for size.
- Provider API keys are profile secrets (PROFILE-4), never logged or returned.
- Only official APIs; the providers' terms are recorded in the dependency/provider ledger.
- No network in tests: providers are tested with `respx` and recorded replies.
- `query` is a template field under WORKFLOW-14: run values come out literal.

## Non-goals
- The picker UI (F-COVER-PICKER).
- Embedding art into files, resizing and format conversion (Open questions 3, 4).
- Discogs, fanart.tv and Last.fm providers (Open question 2).
- Non-front art (back covers, booklets).
- Scraping any site without an official API.

## Open questions
Resolved (author, 2026-11-14: "approved, go with your recommendations"):
1. Art is a ChangeSet change (`TargetChange.art`, additive to the published proposal schema);
   C-CHANGESET and C-JOURNAL gain art handling, so review, apply policy, undo and import undo
   work unchanged.
2. First providers: Cover Art Archive, iTunes, Deezer and local. Discogs, fanart.tv and Last.fm
   are B-059.
3. No embedding in M3; a `beets.command` step can run `beet embedart`. A built-in option is
   B-060.
4. Images are written as downloaded, no new dependency; resizing and format conversion are
   B-061.
5. The step's `apply` defaults to `auto`.
6. `local_counts: true` is the default: a folder image counts as art.

## Tasks
- [x] Core: `CoverQuery`, `CoverCandidate`, `match_score`, `rank`, filters (COVERS-SEARCH-2, -3,
      -7 to -10)
- [x] `rifferr.covers`: protocol, entry points, `CoverHttp`, cache (COVERS-SEARCH-1, -5, -6, -11)
- [x] Providers: coverartarchive, itunes, deezer, local, with recorded replies (COVERS-SEARCH-4)
- [x] ChangeSet `art` change, journal art store, `set_art`/`clear_art` in beets and fake
      backends, contract tests (COVERS-SEARCH-15 to -18, -22)
- [x] `covers.search` step and `cover` review items (COVERS-SEARCH-12 to -14)
- [x] API: search, images, cover decisions, album art (COVERS-SEARCH-19 to -22)
- [x] Profile `covers` settings and validation; regenerate schemas and TS client
- [x] `docs/coverage.md` row 6; example workflow step
