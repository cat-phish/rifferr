# F-COVER-PICKER: Cover search UI
Status: Draft
Milestone: M3

## Context
Architecture §6 asks for a cover picker with:

- a grid that streams results as providers respond;
- an editable query (artist, album, year, catalog number);
- per-provider toggles and weight sliders;
- a full-size preview with dimensions and file size;
- one-click choose.

It also says weights, filters and the enabled-provider order are saved per profile. M3 is
done when "the user can pick art from at least three providers with weights"
(docs/roadmap.md).

S-COVERS-SEARCH built everything below the screen:

- the provider protocol, ranking, cache and art store;
- the `covers.search` step, and cover reviews that hold one album's lane;
- the search route, which streams `covers.candidate`, `covers.provider` and `covers.done`
  on the profile stream (COVERS-SEARCH-19);
- the candidate and art-store image route (COVERS-SEARCH-20);
- the decision route (COVERS-SEARCH-21) and the album art route (COVERS-SEARCH-22).

Its UI is a minimal "Cover choice" card that shows only the candidates stored with the
review. This spec replaces that card with the full picker. It also lets the user change the
cover of any library album outside a run: the album-art item that F-TAG-GRID deferred here.

The reference workflow (docs/original-checklist.md, coverage row 6) picks covers by hand
in foobar2000. With this spec, the albums that `covers.search` couldn't settle on its own
get the same hand pick, in the review queue.

The user can also use an image of their own (open question 4): from a file, by drag and
drop, or by pasting it from the clipboard.

## Behavior
Opening the picker:
- COVER-PICKER-1: A cover review in the review queue (F-REVIEW-QUEUE) MUST show the picker in place of the S-COVERS-SEARCH card. The deep link `/review?item=<review id>` opens it focused. The picker starts with the review's stored candidates, ranked as stored, and doesn't search again until asked.
- COVER-PICKER-2: The library grid (F-TAG-GRID), in album scope, MUST offer "Change cover" on each album row. It opens the same picker in *album mode*: no review, with the query filled from the album's fields through the backend templates `covers.search` uses by default (COVERS-SEARCH-2). The query comes from `POST /api/profiles/{profile}/albums/{id}/cover-query`, which answers from the read-only worker. Album mode starts a search at once.
- COVER-PICKER-3: The picker MUST show the album's current art, when it has some, next to the candidates. In review mode that is the review's `old_sha256` from the art store; in album mode it is `GET /api/albums/{id}/art`. It is labelled "Current".

Query and search:
- COVER-PICKER-4: The query form MUST let the user edit artist, album, year, catalog number and barcode. The MusicBrainz release and release-group ids are shown and can be cleared. "Search" starts `POST /api/covers/search` with that query, the enabled providers, and `review_id` (review mode) or `album_id` (album mode, see -14). "Search again" sets `refresh: true`.
- COVER-PICKER-5: Starting a new search, switching items, or leaving the picker MUST cancel the picker's running search (`DELETE /api/covers/search/{id}`). Events of any other search MUST be ignored.
- COVER-PICKER-6: The grid MUST fill from the profile's event stream as `covers.candidate` events arrive, without waiting for `covers.done`, and MUST NOT poll. When `covers.done` arrives, its `ranked` list replaces the streamed one. Its `dropped` counts show per provider as "n hidden: too small / wrong shape". A `covers.done` with `error` or `cancelled` MUST be shown, and the candidates found until then stay.
- COVER-PICKER-7: Each provider MUST show a status chip from `covers.provider`: searching, done with a count, cached, skipped (needs a secret it doesn't have), or failed with its message. A failed provider never hides the others' results.

Ranking, toggles and weights:
- COVER-PICKER-8: Candidate events and ranked candidates MUST carry `size_score` and `filtered`. `filtered` is the hard-filter reason (`too_small`, `aspect`), or null when the candidate passed. The UI ranks candidates by `weight(provider) × match_confidence × size_score`, with ties broken by provider order and then by area, the same formula and order as COVERS-SEARCH-7. With the profile's weights, its order MUST equal the server's `ranked` order. Filtered candidates are hidden unless "Show hidden" is on.
- COVER-PICKER-9: Each provider in the search MUST have an on/off toggle and a weight slider (0 to 10, step 0.1). Starting values come from the profile's `covers.providers`, and any provider registered but not listed there shows as off. Moving a slider MUST re-rank the grid at once, without a new search. Turning a provider off MUST hide its candidates at once. Turning on one that hasn't been asked in this search MUST start a search of only that provider (`providers: [name]`), and its candidates join the grid.
- COVER-PICKER-10: "Save as defaults" MUST write the toggles, weights and provider order to the profile's `covers.providers` through `PUT /api/profiles/{profile}/covers/providers`. The write changes only that list, keeps the rest of `profile.yaml` (comments and order included, round-tripped with ruamel.yaml), and validates the result as `CoverSettings`. It answers `409 conflict` when `profile.yaml` changed since the picker loaded it: the request carries the file's content hash from `GET /api/profiles/{profile}/covers`. A provider's own `settings` are kept and can't be changed this way. Workers and searches started after the write use the new values.
- COVER-PICKER-11: The provider order MUST be editable by drag or by up/down buttons, and saved by -10. The order breaks ranking ties (COVERS-SEARCH-7).

Preview and choosing:
- COVER-PICKER-12: Clicking a candidate MUST open a preview with the full-size image from `GET /api/covers/image/{id}`, the provider, the title, a link to the source page, and the real dimensions and file size. The dimensions come from the loaded image and the size from the downloaded bytes. The source link opens in a new tab with `rel="noopener noreferrer"`. A failed download MUST be shown in the preview, and choosing that candidate is then refused.
- COVER-PICKER-13: In review mode, "Use this cover" (on the card or in the preview) MUST send `choose` with that candidate (COVERS-SEARCH-21), and "No cover" and "Fail this album" stay. After the 202 the picker shows "Setting the cover", and shows the outcome from `cover.resolved` and the job's end. A refusal (`422 bad_image`, `409 busy` or `409 already_resolved`) MUST be shown on that candidate, and the others stay choosable.
- COVER-PICKER-14: In album mode, "Use this cover" MUST call `POST /api/profiles/{profile}/albums/{id}/cover {candidate_id, old_sha256}`. The server downloads and checks the image (as COVERS-SEARCH-21), keeps it in the art store, and queues one `manual_apply` job. That job applies a manual ChangeSet (source `manual` / `ui`, CHANGESET-14) on the album's `art_sha256`, with `old` as sent, so a cover changed meanwhile comes back as a conflict (CHANGESET-8). The change then appears in "Recent edits" with Undo (TAG-GRID-11). A local candidate MUST be an image in that album's folder, which the server learns from the read-only worker, never from the client. `POST /api/covers/search` takes `album_id` for the `local` provider in the same way.
- COVER-PICKER-15: The picker MUST work by keyboard alone. Candidates are focusable in rank order, Enter opens the preview, and the preview's "Use this cover" is its default button. Every image has alt text naming the provider and size.

Own images (upload and paste):
- COVER-PICKER-17: The picker MUST accept an image of the user's own in three ways: "Upload image" (a file picker), dropping an image file on the picker, and pasting image data from the clipboard (Ctrl/Cmd+V while the picker has focus, or the "Paste image" button through the Clipboard API). Each sends `POST /api/covers/uploads {profile, data}`, where `data` is the image base64-encoded in JSON, so the route stays JSON-only (F-SECURITY). Several images at once are sent one by one.
- COVER-PICKER-18: The upload route MUST decode the image and check it as a downloaded one: JPEG or PNG by magic bytes, at most `covers.max_bytes`, and the real size from its header. It keeps the bytes in the cover cache and answers a candidate with provider `upload`, `match_confidence` 1.0, its size, and a `size_score` and `filtered` like any other. The candidate joins the grid at its score (weight 1.0 unless the profile lists `upload`; it isn't a searchable provider) and can be previewed and chosen through -13 or -14 like any other. A bad image answers `422 bad_image` with the reason, and an image that is too small or the wrong shape is still accepted but shown as filtered, so the user can see why.
- COVER-PICKER-19: Pasted text MUST NOT be fetched. When the clipboard holds a URL or other text and no image, the picker says "Paste an image, not a link: copy the image itself". The server never fetches a client-sent URL (COVERS-SEARCH-20).

Library grid:
- COVER-PICKER-16: The library grid in album scope MUST have an "Art" column, shown by default and hideable like the others (TAG-GRID-3), with a small thumbnail of each album's current art. Thumbnails load lazily, only for visible rows, from `GET /api/albums/{id}/art`, which sends `Cache-Control: private, max-age=3600` and an `ETag` of the art's sha256. An album without art shows an empty cell.

## Interfaces
```python
# rifferr.core.covers: added fields (schema additions only)
class RankedCandidate(CoverCandidate):
    score: float
    size_score: float                     # COVER-PICKER-8
    filtered: Literal["too_small", "aspect"] | None = None

# covers.candidate event data: {search_id, candidate: CoverCandidate, size_score, filtered}

# rifferr.api.models
class CoverSearchBody(_Model):            # existing, plus
    album_id: str | None = None           # album mode; exclusive with review_id

class CoverQueryAnswer(_Model):           # POST /api/profiles/{profile}/albums/{id}/cover-query
    query: CoverQuery                     # folder omitted
    label: str
    art_sha256: str | None

class AlbumCoverBody(_Model):             # POST /api/profiles/{profile}/albums/{id}/cover
    candidate_id: str
    old_sha256: str | None

class AlbumCoverQueued(_Model):           # 202
    changeset_id: str
    job_id: str
    sha256: str

class CoverUploadBody(_Model):            # POST /api/covers/uploads
    profile: str
    data: str                             # base64; decoded size <= covers.max_bytes

class CoverUploaded(_Model):              # 201
    candidate: RankedCandidate            # provider "upload"

class CoverProvidersView(_Model):         # GET /api/profiles/{profile}/covers
    providers: list[ProviderSetting]      # settings values with secrets left out
    registered: list[str]                 # every installed provider
    profile_hash: str                     # sha256 of profile.yaml as read

class CoverProvidersBody(_Model):         # PUT /api/profiles/{profile}/covers/providers
    providers: list[ProviderOrder]        # name, enabled, weight; order = list order
    profile_hash: str
```

UI states of the picker: `idle` (stored candidates only), `searching`, `done`, `error`,
`choosing`, `chosen`, `failed`. Components: `CoverPicker` (query form, provider panel,
grid), `CoverPreview` (modal), `ProviderPanel` (toggles, sliders, order, save). The
ranking function is `rankCandidates(candidates, providers)` in
`frontend/src/covers/rank.ts`.

## Examples
Review mode. "Boards of Canada - Geogaddi" waits because nothing scored 0.6 or more. The
user opens it from the notification and the picker shows the three stored candidates. They
change the album to "Geogaddi (Remastered)" and press Search:

- Cover Art Archive's chip turns "searching" and then "done, 0".
- Deezer streams one 1000×1000 candidate.
- iTunes's 600×600 candidate shows "score 0.60".

They drag the iTunes weight to 2 and its card moves first, with score 1.20. They open its
preview (600×600, 84 KB) and press "Use this cover". The queue item turns "Setting the
cover", then disappears, and the album's lane goes on.

Album mode. In the library grid, the user picks "Change cover" on an album that has art.
The picker shows "Current" (500×500) and searches at once. They choose a 1200×1200 Cover
Art Archive image, and "Recent edits" lists `art_sha256` changed by `ui`, with Undo.

Saving defaults. The user turns Deezer off, sets Cover Art Archive to 3 and saves. The
`covers.providers` list in `profile.yaml` is rewritten with comments elsewhere kept, and
the next `covers.search` step uses weight 3.

## Extensibility
- A third-party provider (`rifferr.cover_providers` entry point) appears in the provider
  panel with no UI change. Its status, candidates and weight work like the built-ins.
- The picker is one component used by the review queue and the library grid. Other screens
  (a future album page) reuse it through the same two modes.
- Fixed: the score formula (shared with COVERS-SEARCH-7), the image route as the only image
  source, and choosing through the decision route or a manual ChangeSet.

## Security & safety
- The browser loads images only from Rifferr's own image routes. Candidate URLs are never
  put in an `<img src>`, so a provider can't make the browser contact other hosts. Source
  page links are plain links the user clicks, with `noopener noreferrer`.
- Album mode learns the album's folder from the read-only worker. A client-sent folder is
  ignored, as in COVERS-SEARCH-19.
- `PUT …/covers/providers` changes only the enabled flag, weight and order of the listed
  providers. It can't add provider `settings` or secrets, change hosts, or touch any other
  key. It is JSON-only behind the F-SECURITY middleware. It is the first route that writes
  `profile.yaml` (open question 1).
- Provider settings returned by `GET …/covers` leave out secret references (PROFILE-4).
- Uploads arrive as base64 JSON, so no multipart or other non-JSON body is accepted
  anywhere. The request body is capped at `covers.max_bytes` × 4/3 plus a small overhead
  before decoding. Uploaded bytes are checked by magic bytes, served only through the image
  route with its no-script CSP, and never parsed beyond the header.
- A cover change outside a run is a manual ChangeSet: it is journaled and undoable, and
  the art store keeps the old art (COVERS-SEARCH-17).

## Non-goals
- Cropping, resizing, format conversion or embedding (B-060, B-061).
- Editing a provider's own settings (sizes, match rules) or the size filters from the
  picker. Those stay in `profile.yaml`.
- Back covers and other non-front art.
- Searching the cover of several albums at once. Each album gets its own picker.
- Fetching an image from a pasted or typed URL (COVER-PICKER-19).

## Open questions
All resolved by the user on 2026-11-14:
1. ~~Writing `profile.yaml` from the UI?~~ Yes, for `covers.providers` only, with the
   content-hash guard and ruamel round-trip (COVER-PICKER-10).
2. ~~How running workers learn new weights?~~ The profile is read again when each run and
   each search starts. A running run keeps its values. If workers cache the profile, the
   supervisor restarts an idle worker after the write.
3. ~~Art column default?~~ Shown by default, lazy, with ETag caching (COVER-PICKER-16).
4. ~~Uploading an image?~~ Yes, including copy and paste (COVER-PICKER-17 to -19). It is
   sent as base64 JSON so every mutating route stays JSON-only.
5. ~~Album mode at once or through review?~~ At once, as a manual edit (COVER-PICKER-14).

## Tasks
- [ ] `size_score` and `filtered` on candidates and candidate events; schemas, TS client
- [ ] `cover-query`, album `cover` route, `album_id` in search; manual ChangeSet on `art_sha256`
- [ ] `GET`/`PUT` profile covers with ruamel round-trip and hash guard; profile reload
- [ ] album art route ETag and caching headers
- [ ] `rank.ts` with a test matching the server's ranking
- [ ] `CoverPicker`, `ProviderPanel`, `CoverPreview`; review queue uses the picker
- [ ] upload route (base64 JSON, checks, `upload` candidates); file picker, drop and paste in the picker
- [ ] library grid: "Change cover" and the "Art" column
- [ ] Playwright: review-mode pick through the `local` provider (no network), weight re-rank, paste an image, album-mode change and undo
- [ ] coverage row 6 note, roadmap, overview status
