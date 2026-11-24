# C-ALBUM-FACTS: Computed album facts in templates and conditions
Status: Implemented
Milestone: M5

## Context
Architecture §4. Tag-script conditions are beets queries over one album's fields, so they
can't see anything that depends on the album's tracks: how many there are, how long the album
is, how many album artists it has, or how many tracks have more than one artist. The reference
workflow needs these to:
- suggest split / collaboration / soundtrack / compilation (author, 2026-11-23);
- flag albums with at most 6 tracks and under 30 minutes as likely EPs (B-044).

TEMPLATE-FUNCTIONS-5 keeps template functions pure, so facts can't be template functions that
read the library. beets has a public mechanism for computed album fields:
`BeetsPlugin.album_template_fields` (getters that receive the `Album`). Rifferr's beets plugin
registers built-in facts and user-named counts there. The `FakeBackend` computes the same facts
from its snapshots.

## Behavior
- ALBUM-FACTS-1: The backend MUST provide these computed album fields in templates:
  `rf_items` (track count), `rf_length` (total length in whole seconds), and
  `rf_count_<field>` for list fields (`rf_count_albumartists` = number of album artists).
- ALBUM-FACTS-2: A profile MAY name item counts in the beets config:
  `rifferr.album_facts: {<name>: <beets item query>}`. Each becomes `rf_<name>`, the number of
  the album's items the query matches.
- ALBUM-FACTS-3: Facts MUST be computed from the working values when a tag script runs, so an
  earlier action in the same script is seen (as TAGSCRIPT conditions see earlier actions).
- ~~ALBUM-FACTS-4~~ (amended 2026-11-23 while implementing: facts are typed integers, so
  `when` queries compare them directly, e.g. `rf_items:..6 rf_length:..1799`, on FakeBackend and
  beets alike; FakeBackend has no template functions, so `%ifgreater` conditions couldn't be
  tested without beets. No `if:` key was added.) Was: A tag-script action MAY have `if:` (a beets template) next to or instead of
  `when:`. The action runs only when `when` matches and `if` evaluates to non-empty text other
  than `0` or `false`. This is how conditions use facts: `if: "%ifgreater{$rf_count_albumartists,1,yes}"`.
- ALBUM-FACTS-5: Facts are read-only: an action that sets an `rf_` field MUST be a validation
  error.
- ALBUM-FACTS-6: `FakeBackend` MUST compute the same facts, so engine and step tests need no
  beets.
- ALBUM-FACTS-7: Artist-name rules MAY be set in the beets config
  (`rifferr.artist_names: <file>`, relative to the profile's `rules/` folder): `joiners`, a list
  of texts that may join two artist names (" & ", " / ", " - ", " and ", " x ", " vs. ", " + ",
  ", ", " with ", …, all the user's), and `names`, artist names known to be one artist even
  though they contain a joiner ("AC/DC", "Simon & Garfunkel"). Nothing is built in
  (author, 2026-11-23).
- ALBUM-FACTS-8: `rf_joined_albumartist` MUST be `1` when the album artist contains a joiner
  outside every known name, else `0`.
- ALBUM-FACTS-9: A track MUST count as having several artists when at least two entries of its
  `artists` list occur in its `artist` text (MusicBrainz credits; featured artists no longer
  occur there once `ftintitle` moved them into the title), or, when `artists` has fewer than
  two entries, when `artist` contains a joiner outside every known name.
  `rf_multi_artist_tracks` counts such tracks and `rf_share_multi_artist_tracks` is their
  share of the album's tracks, 0–100, rounded down.
- ALBUM-FACTS-10: `rf_share_<name>` MUST exist for every named count of ALBUM-FACTS-2 too.

## Interfaces
```yaml
# beets config
rifferr:
  album_facts:
    multi_artist_tracks: "artists::.+"   # placeholder; see open question 1
```
```python
class ActionBase(BaseModel, frozen=True):
    when: str | None = None
    if_: str | None = Field(None, alias="if")   # new
    ...
```

As implemented (2026-11-23; pure `rifferr.core.facts`, beets config in
`rifferr.backends.beets.facts`):
- `LibraryBackend.album_facts(album)` and `artist_names()`; the tag-script `Evaluator` gained
  `album_facts`. The evaluator adds an album's facts to the album target's working values only
  when a `when` query or template names `rf_`, and recomputes them each time from the current
  working values (ALBUM-FACTS-3). Item targets don't see them.
- Facts exist only where Rifferr evaluates: tag scripts (and `tags.choose`). They are not
  `album_template_fields`, so `beet` subprocesses and `library.move` path formats don't see
  them (a getter on a stored album couldn't see working values).
- The `rifferr` beets plugin types every fact it can know in advance as an integer
  (`album_types`): built-ins, named counts and their shares, and `rf_count_<field>` for every
  beets list field and every `multi` file tag.
- `rf_count_<field>` exists only when the album has the list field.
- `rifferr.artist_names` is relative to the beets directory (or absolute), not the profile's
  `rules/` folder: the backend knows only its beets directory. It is re-read when its mtime
  changes. Matching of joiners and known names ignores case.
- Setting an `rf_` field is a script error when the action runs (TAGSCRIPT-15), shown in
  preview, rather than a parse-time validation error.
- B-044: `examples/scripts/likely-ep.yaml` (`release-suffixes.yaml` step `likely-ep`,
  `apply: review`, limits in that workflow's vars `ep_max_tracks` / `ep_max_seconds`).

## Examples
- Album with 3 album artists → `$rf_count_albumartists` = `3`.
- B-044: `if: "%ifgreater{7,$rf_items,%ifgreater{1800,$rf_length,yes}}"` is true for 6 tracks
  and 25 minutes.
- Collaboration rule: `if: "%ifgreater{$rf_share_multi_artist_tracks,50,yes}"`
  (assumes the `rf_share_` facts of open question 2).

## Extensibility
Plugin authors add facts through the same beets plugin hook. Rifferr's `rf_` names are fixed.

## Security & safety
Pure reads over the album's items; no filesystem or network.

## Non-goals
Arbitrary arithmetic in templates.

## Open questions
1. ~~How to detect several artists on a track?~~ Resolved 2026-11-23 (author): like split
   joiners. MusicBrainz credits first, joiners and known names as fallback (ALBUM-FACTS-7..9).
2. ~~"Most tracks"?~~ Resolved 2026-11-23: `rf_share_` facts (ALBUM-FACTS-9, -10).
3. Should `if:` also be allowed on `tags.choose` options (S-TAGS-CHOOSE) and `library.move`
   path rules?

## Tasks
- [x] FakeBackend facts and tests
- [x] beets facts (typed via `album_types`, not `album_template_fields`) and tests
- [x] ~~`if:` on tag-script actions~~ typed facts in `when` instead (ALBUM-FACTS-4 amended)
- [x] Use for B-044 in an example script
