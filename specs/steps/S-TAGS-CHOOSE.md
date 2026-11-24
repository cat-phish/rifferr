# S-TAGS-CHOOSE: `tags.choose` step (a review with choices)
Status: Implemented
Milestone: M5

## Context
Architecture §2a and §3. A `review` gate can only continue or reject, and a `tags.script`
ChangeSet can only be accepted (in part) or rejected. Some decisions need a person to pick
one of several outcomes, with the likely one preselected, or type a value. The reference
workflow needs this for (author, 2026-11-23):
- album kind: split, collaboration, soundtrack or compilation, for every album with several
  album artists and every soundtrack or compilation;
- the broad genre bucket, for every album, with a custom entry;
- dates still missing after the copy rules (custom entry only).

Each option is a list of tag-script actions, so the result is an ordinary ChangeSet
(non-negotiable 3) that goes through the engine, the journal and undo. Only the album's lane
waits (non-negotiable 6).

## Behavior
- TAGS-CHOOSE-1: For each album the step's query matches, the step MUST evaluate every option's
  actions against the album (`rifferr.tagscript.run_script`) and keep the options that change
  something, plus `custom`.
- TAGS-CHOOSE-2: The step MUST mark as suggested the first option, in `options` order, whose
  `suggest` condition holds (a `when` query, which may use album facts, C-ALBUM-FACTS).
  With none, nothing is preselected.
- TAGS-CHOOSE-3: The step MUST open one choice review per album on that album's lane and send
  `review.requested`; other albums continue.
- TAGS-CHOOSE-4: A choice review MUST show the album, the `show` fields, each option's label
  with a preview of its field changes, the suggestion preselected, and for `custom` an input
  per field typed by the backend's field type (lists as tag inputs).
- TAGS-CHOOSE-5: Deciding MUST be `POST /api/choices/{id}/decision {option | custom, values?,
  note?}`. The chosen option's actions (or the custom values, as `set` actions) MUST be proposed
  as one ChangeSet for the album and applied through the engine at once, journaled with the
  option's label as rationale.
- TAGS-CHOOSE-6: `skip` MUST leave the album unchanged and continue its lane; `reject` MUST
  follow `on_reject` (`stop` the album's lane or `continue`).
- TAGS-CHOOSE-7: With `when_single: auto`, an album with exactly one changing option and no
  `custom` MUST apply that option without review. Default `review`.
- TAGS-CHOOSE-8: Options MAY come from a rules file (`options_from`), each rule giving a
  label, a beets query and actions. Rules are ranked by how many of the album's values in
  `rank_by` (a list field) they match; ties keep file order.
- TAGS-CHOOSE-9: A rules file MAY set `fallback: {template: ...}`: for values no rule matches,
  each value's template result becomes an extra option label with the rule's actions
  interpolated (`{{ choice }}`). The backend provides `%genre_root{value}` (the top-level parent
  in beets lastgenre's bundled genre tree; the value itself when not in the tree).
- TAGS-CHOOSE-10: Rules files and the choices a person makes with `custom` MAY be remembered:
  with `learn: true`, a custom decision that names a value from `rank_by` with no rule appends
  a rule to the rules file (ruamel round trip, comments kept), shown in the decision response.
- TAGS-CHOOSE-13: A choice review MAY offer `known_name: <field>`: a "This is one artist"
  action that adds the field's current value to the artist-name rules' `names`
  (C-ALBUM-FACTS ALBUM-FACTS-7; ruamel round trip, comments kept) and decides the review with
  the option given by `known_name_option`. Later albums with that name are no longer flagged.
  The rules file can also be edited by hand.
- TAGS-CHOOSE-11: The step's outputs: `choices` (per album: offered, suggested, chosen,
  decided_by), `changesets`.
- TAGS-CHOOSE-12: A Playwright test MUST cover choosing a non-suggested option, a custom value,
  and undoing the run.

## Interfaces
```python
class ChoiceOption(BaseModel, frozen=True):
    label: str
    suggest: Suggest | None = None       # when
    actions: list[Action]                # tag-script actions

class TagsChooseParams(BaseModel):
    query: str | None = None
    message: str | None = None           # beets template
    show: list[str] = []
    options: list[ChoiceOption] = []
    options_from: str | None = None      # rules file, relative to <profile>/rules/
    rank_by: str | None = None           # list field for rules ranking
    custom: list[str] = []               # fields offered for free entry
    when_single: Literal["review", "auto"] = "review"
    on_reject: Literal["stop", "continue"] = "stop"
    learn: bool = False
```
Rules file:
```yaml
target: genres                 # what the actions set, for fallback
rules:
  - label: Rock
    when: 'subgenres::(?i)rock|punk|grunge'
    actions: [{ set: { genres: [Rock] } }]
fallback:
  template: "%genre_root{{{ value }}}"
  actions: [{ set: { genres: ["{{ choice }}"] } }]
```

As implemented (2026-11-23; `rifferr.steps.tags_choose`, `rifferr.choice_rules`, the
worker's `choice_apply` job):
- The review is a `ChoiceReview` (`rifferr.core.choices`) in the `choice_reviews` table. The
  engine emits `choice.requested` / `choice.resolved` (like `cover.requested`), not
  `review.requested`, whose data describes a review gate. Lanes hold on kind `choice`, so
  `review.needed` notifications include it.
- Each option is stored with its actions; the decision job runs them again on the album's
  current values, then proposes and applies the ChangeSet (source `tags.choose`, rationale
  "<label> (chosen in review)[: note]"). Custom values become `set` actions with the values
  made literal (`escape_template`), or `delete` for an empty value.
- `GET /api/choices/{id}` and `POST /api/choices/{id}/decision {decision: choose | skip |
  reject, option?, values?, known_name?, note?}`; `option: custom` takes `values`. A decision
  that doesn't fit the review (unknown option or field) is a 400 at once.
- `reject` with `on_reject: stop` ends the album's lane `rejected`.
- `query` is matched with album facts (the step's `when` can't see them). The step's default
  `apply` is `auto` (the choice was the review); with `when_single: auto` the only changing
  option is proposed through that policy.
- Rules options: a rule is offered when it matches at least one `rank_by` value (each value
  tried alone) and changes something; rules and fallback options are ranked together by
  their count, ties in file order. With no `suggest` match, the top-ranked rule is suggested.
  Rules files are data: `{{ value }}` / `{{ choice }}` are the only placeholders, not
  workflow interpolation. A path leaving `rules/` fails the step.
- `%genre_root` reads `beetsplug/lastgenre/genres-tree.yaml` as a file (importing the plugin
  needs `pylast`) and title-cases the root, as lastgenre writes genres by default.
- `learn` (TAGS-CHOOSE-10): a custom decision adds one rule labelled with the custom value of
  the rules file's `target`, whose query matches the album's unmatched `rank_by` values
  exactly (`rank_by::^v1$|^v2$`). It needs `options_from`, `rank_by`, `custom` and a
  `target`.
- `known_name_option` (TAGS-CHOOSE-13) may name an option, whose actions then apply; a label
  that names no option (the example's "Single artist") leaves the album unchanged. The name is
  added through `LibraryBackend.add_artist_name` to the file `rifferr.artist_names` names;
  that file must be writable. The button shows only when the field has a single value.
- The plugin API is `ctx.request_choice(ChoiceRequest)` (like `ctx.request_cover`).
- Not done: timeouts (open question 3).

## Examples
Album kind (foobar-style):
```yaml
- id: album-kind
  uses: tags.choose
  with:
    query: "albumtypes::soundtrack|compilation , albumartists::.+;.+ , rf_joined_albumartist:1"
    known_name: albumartist
    known_name_option: Single artist
    show: [albumartist, albumartists, albumtypes]
    options:
      - label: Collaboration
        suggest: { when: "rf_share_multi_artist_tracks:51.." }
        actions: [{ set: { albumkind: collab, albumartist: "{{ vars.label_collab }}" } }]
      - label: Split
        suggest: { when: "rf_count_albumartists:2..4" }
        actions: [{ set: { albumkind: split, albumartist: "{{ vars.label_split }}" } }]
      - label: Soundtrack
        suggest: { when: "albumtypes:soundtrack" }
        actions: [{ set: { albumkind: soundtrack, albumartist: "{{ vars.label_soundtrack }}" } }]
      - label: Compilation
        actions: [{ set: { albumkind: compilation, albumartist: "{{ vars.label_compilation }}" } }]
    custom: [albumartist, albumartists, albumkind]
```

## Extensibility
Options are data. Plugin authors reuse the choice review from their own steps through
`ctx.request_choice(...)` (public step API, like `ctx.request_cover`).

## Security & safety
Option actions are tag-script actions: no code runs. Custom values are validated and coerced
like tag-grid edits. Values render as text (REVIEW-QUEUE-17). Rules-file writes (`learn`) stay
inside `<profile>/rules/`.

## Non-goals
- Merging several steps' reviews into one card (author, 2026-11-23: separate cards).
- AI suggestions (S-AI-TAG).

## Open questions
1. ~~Where do the individual album artists come from?~~ Resolved 2026-11-23 (author): from
   MusicBrainz's `albumartists` when it lists several; otherwise an album artist with a joiner
   (`rf_joined_albumartist`) always goes to review, its proposed `albumartists` split at the
   joiners and editable, with "This is one artist" (TAGS-CHOOSE-13) for names like "AC/DC".
2. ~~`albumartist_sort`?~~ Resolved 2026-11-23 (author): it becomes the label too.
3. Timeouts: should a choice review support `timeout`/`on_timeout` like `review`?

## Tasks
- [x] Model, step and validation; FakeBackend tests per behavior
- [x] Choice store and decision job; API route; SSE events
- [x] Review card (radio options, previews, custom inputs)
- [x] Rules file, ranking, `%genre_root` (backends/beets) and `learn`
- [x] Playwright spec
