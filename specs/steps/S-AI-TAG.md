# S-AI-TAG: `ai.tag` step: AI-proposed field values as ChangeSets
Status: Implemented
Milestone: M4

## Context
Architecture §7. Some tagging needs judgment that a rule can't capture well. `ai.tag` asks a
language model to propose values for a fixed list of fields, and turns the answer into ordinary
ChangeSets (C-CHANGESET), which wait in the review queue (S-REVIEW, F-REVIEW-QUEUE) like any
other proposal.

The step is the `tag` AI task on the shared AI core (C-AI-CORE). Providers, keys, the HTTP
allowlist, retries, the cache, events, usage, the preview, vocabulary matching and the safety
report all come from there. This spec covers only what is specific to the step: the field
allowlist, what is sent, the validation order and the proposals.

The model never writes anything (non-negotiable 4). Which fields, vocabulary, rules and
provider to use are all the user's choice (non-negotiable 1). For the genre bucket, the
reference workflow uses AI suggestions on its choice review (F-AI-SUGGEST), not this step.

Split from the 2026-11-18 draft (author, 2026-11-26). Behavior IDs were renumbered; no tests
named the old ones.

## Behavior
- AI-TAG-1: `ai.tag` MUST declare `force_review = True` (CHANGESET-6): `apply: auto` on it is a
  validation error, and `apply` defaults to `review`. Its ChangeSets MUST have
  `source.kind = ai` and `source.name = "ai.tag"` (CHANGESET-1).
- AI-TAG-2: `with.fields` MUST be a non-empty list of field names: the allowlist. Each field
  MUST be one the backend knows, or a flexible attribute name that is valid for the backend
  (checked with `backend.check` at run time and by name shape at validation time).
  `with.scope` is `album` (default) or `item`. With `album`, proposals target the album. With
  `item`, they target items, and the model sees and answers for every track of the album in
  one request.
- AI-TAG-3: The step MUST make at most one task run per album and lane (C-AI-CORE handles the
  retries). Albums run concurrently up to `ai.concurrency`. One album's failure MUST NOT affect
  other albums' lanes (C-LANES).
- AI-TAG-4: `with.when` MAY be a beets query. Albums it doesn't match are skipped without a
  request. `with.only_empty: true` MUST skip an album whose allowlisted fields already all have
  a non-empty value.
- AI-TAG-5: The request MUST contain only: the task's prompt template, the text of `with.rules`
  (if set), each allowlisted field's vocabulary (if set), the field allowlist with each field's
  current values, and the values of the `with.context` fields for the album (and, with
  `scope: item` or `with.item_context` set, for each item). The task's `sends` (AI-CORE-8)
  lists exactly these.
- AI-TAG-6: `with.rules` names a Markdown file, and `with.vocabulary` and `with.denylist` map an
  allowlisted field to a list file: one value per line, blank lines and lines starting with
  `#` ignored. All of them follow AI-CORE-12. A vocabulary or denylist for a field that isn't in
  `fields` MUST be a validation error.
- AI-TAG-7: Each target in the answer whose id the request didn't list MUST be ignored and
  logged. For each proposed field value, the step MUST check, in this order:
  1. A field not in `fields` is dropped (`field_not_allowed`).
  2. A value that matches a denylist entry (AI-CORE-13 match key) is dropped (`denylisted`).
  3. When the field has a vocabulary, the value is matched with AI-CORE-13 in mode
     `with.on_unknown_value`: `drop` (default, mode `strict`) or `flag` (mode `flag`).
  4. A confidence below `with.min_confidence` (default 0) is dropped (`low_confidence`).
  5. The value MUST be converted with `backend.coerce` and pass `backend.check`. A list is
     allowed only for a multi-value field (CHANGESET-2). A value that fails is dropped
     (`invalid_value`).
  For a multi-value field each element is checked on its own. If every element is dropped, the
  field change is dropped. A value equal to the current value is a no-op (CHANGESET-4).
- AI-TAG-8: When attempts run out (AI-CORE-14), the album is handled by `with.on_error`:
  `skip` (default) adds a run warning and proposes nothing, which is safer for unattended
  runs; `fail` fails that album's lane.
- AI-TAG-9: When validation leaves at least one field change for an album, the step MUST
  propose them with one `ctx.propose` call for that album (CHANGESET-14). Otherwise it proposes
  nothing for it and logs why.
- AI-TAG-10: `FieldChange` MUST gain two optional fields, `confidence` (0 to 1) and `warnings`
  (list of strings), and the published proposal schema change MUST be additive. `ai.tag` fills
  each field's `rationale` and `confidence` from the answer, and the ChangeSet's `rationale`
  with the provider name, model and prompt version. The review view MUST show each field's
  rationale, confidence and warnings. A field change with a warning MUST start unticked, so
  accepting the whole ChangeSet leaves it out unless the reviewer ticks it.
- AI-TAG-11: The step's outputs MUST include `requests`, `cached`, `proposed`, `rejected`,
  `input_tokens`, `output_tokens` and `cost` (sums of the task results' usage, AI-CORE-15).
  The run page MUST show the token totals, and the cost when known.
- AI-TAG-12: Each ChangeSet MUST keep the request it came from as `ai_input`
  (`{task, provider, input}`, the task's exact input). The review view of an `ai.tag`
  ChangeSet MUST link to the preview (AI-CORE-18) of that input. A re-proposed ChangeSet
  keeps it.
## Interfaces
```yaml
- id: genres
  uses: ai.tag
  apply: review                    # the default; `auto` is a validation error
  with:
    provider: claude               # optional, one of the profile's providers (AI-CORE-7)
    scope: album                   # album | item
    fields: [genre]                # allowlist
    context: [albumartist, album, year, label, genre, mb_albumid]
    item_context: []               # optional item fields, e.g. [title]
    rules: rules/genres.md         # relative to the profile directory
    vocabulary: {genre: rules/genre-allowlist.txt}
    denylist: {genre: rules/genre-denylist.txt}
    on_unknown_value: drop         # drop | flag
    min_confidence: 0.5
    only_empty: false
    when: "genre::."               # optional beets query
    retries: 2
    on_error: skip                 # skip | fail
    refresh: false
```
```jsonc
// output schema of the `tag` task (scope: album). With scope: item, "id" is an item id.
{
  "targets": [{
    "id": "album",
    "fields": [{
      "field": "genre",
      "value": "Post-Punk",                // string, or list of strings for multi-value
      "confidence": 0.82,                  // 0..1
      "rationale": "…"                     // at most 500 characters
    }]
  }]
}
```
- `FieldChange` gains `confidence: float | None = None` and `warnings: list[str] = []`
  (additive change to `schemas/changeset-proposal.schema.json`).
- `ChangeSet` gains `ai_input: {task, provider, input} | None` (additive). Steps set it
  with `ctx.propose(proposal, ai_input=ChangeSetAI(...))`; it isn't part of the proposal
  schema, so shell steps can't set it.
- Modules: `rifferr.ai.tasks.tag` (the `AITask`), `rifferr.steps.ai_tag` (the step).

## Examples
`rules/genre-allowlist.txt` holds `Rock`, `Post-Punk`, `Electronic`. Album with
`genre: "post punk; darkwave"`. The model answers:
```json
{"targets": [{"id": "album", "fields": [
  {"field": "genre", "value": ["post-punk", "Darkwave"], "confidence": 0.8, "rationale": "…"},
  {"field": "mood", "value": "gloomy", "confidence": 0.9, "rationale": "…"}]}]}
```
- `on_unknown_value: drop` → proposes `genre: ["Post-Punk"]`; `ai.rejected` for `Darkwave`
  (`not_in_vocabulary`) and `mood` (`field_not_allowed`).
- `on_unknown_value: flag` → proposes `genre: ["Post-Punk", "Darkwave"]` with warning
  `not_in_vocabulary`; that field change starts unticked in review.
- `apply: auto` on the step → validation error "ai.tag always goes to review".

## Extensibility
- Everything domain-specific is the user's: fields, context, rules, vocabularies, denylists,
  thresholds, provider and model.
- Fixed: the output schema, the validation order (AI-TAG-7), `force_review`, and what may be
  sent (AI-TAG-5).

## Security & safety
See C-AI-CORE. The step adds the field allowlist: whatever the model answers, only `fields`
can change, and only after review.

## Non-goals
- Genre-bucket suggestions on a choice review (F-AI-SUGGEST).
- Auto-applying AI proposals.

## Open questions
1. ~~`apply: auto` allowed for AI output?~~ Resolved 2026-11-18 (author): no (AI-TAG-1).
2. ~~`on_error` default?~~ Resolved 2026-11-26 (author): `skip` plus a run warning
   (AI-TAG-8).
3. Should a flagged (`not_in_vocabulary`) value offer "add to vocabulary" in review, which
   edits the vocabulary file after confirmation? Or leave that to F-AI-CHAT?
4. One request per album (this draft), or several small albums per request to save tokens?
   Batching makes caching and per-album failure coarser.
5. ~~AI-TAG-12 preview input?~~ Resolved 2026-11-26 (author): the ChangeSet keeps the
   task input as `ai_input`, and review previews that (AI-TAG-12).

As implemented (2026-11-26; `rifferr.steps.ai_tag`, `rifferr.ai.tasks.tag`,
`rifferr.core.ai_validate.check_field_values`):
- Checks 1 to 4 of AI-TAG-7 are pure (`check_field_values`, run by the task's `validate`);
  check 5 runs in the step. A field answered twice keeps its first answer
  (`duplicate_field`). Without a vocabulary, values still take the profile's lookalike
  characters (AI-CORE-22).
- A list answer is checked element by element as `[value]` with `backend.check`, uncoerced,
  since `coerce` passes lists through. A single value is coerced first. The prompt says which
  fields hold several values; the step asks the backend once per field, by checking a
  one-element list.
- Step types gain an optional `source_kind = "ai"` attribute, giving `source.kind = ai`
  (public step API, non-negotiable 7). `ctx.ask_ai` gains `retries` and `refresh`, and
  `ctx.ai_rejected` reports step-side drops as `ai.rejected`. A task's `build` may raise
  `ValueError` (e.g. a vocabulary with two entries that match alike), and the runner reports
  that as the task failing.
- Outputs also include `skipped`, `failed` and `currency`; `cost` is `""` when any request's
  cost is unknown. The safety report lists `fields` among the data sent.
- In review, changes with warnings start unticked, and the queue's bulk Accept leaves them
  out too. A ChangeSet whose changes all have warnings is left waiting.

## Tasks
- [x] `tag` AI task: prompt, output schema, `sends` (AI-TAG-5, -6)
- [x] Validation order in `core.ai_validate` with Hypothesis tests (AI-TAG-7)
- [x] `ai.tag` step: lanes, `when`, `only_empty`, `on_error`, proposals (AI-TAG-1 to -4, -8, -9)
- [x] `FieldChange.confidence` / `warnings`, schema and TS client regenerated (AI-TAG-10)
- [x] Outputs and run page usage (AI-TAG-11)
- [x] `ai_input` on the ChangeSet; review-view preview link (AI-TAG-12)
