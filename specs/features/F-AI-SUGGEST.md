# F-AI-SUGGEST: AI suggestions on a choice review
Status: Draft
Milestone: M4

## Context
Architecture §7. A `tags.choose` review (S-TAGS-CHOOSE) offers options ranked from a rules
file. The reference workflow's `genre-bucket` step ranks the user's genre buckets by the album's
`subgenres`. When the rules are unsure, the person has to work out the bucket by hand: an album
with no sub-genres, one whose sub-genres only reach the fallback (`%genre_root`), or one where
two buckets tie. BACKLOG B-098 asked for an "AI suggest" button.

The author's decisions (2026-11-26):
- The rules ranking runs first, as today. The AI adds suggestions, it never replaces the rules.
- Each choice review has an **Ask AI** button. The review queue has a **Suggest with AI for
  all** button at the top.
- The AI gets the existing options (for genre-bucket: the bucket list), but isn't limited to
  them. It may suggest values that aren't options, and it may suggest several, ranked.
- An automatic mode runs the AI when the rules are unsure. Whether and when it runs is the
  user's setting, on the Settings page (F-SETTINGS).
- Suggestions must use the user's own characters in names, e.g. `Alt／Indie` with the
  fullwidth slash, never a near-copy `Alt/Indie` (AI-CORE-13, -22).

This is the `choice.suggest` AI task on C-AI-CORE. Nothing here is specific to genres: any
`tags.choose` step can turn it on, and the step decides what is sent.

## Behavior

### Step configuration
- AI-SUGGEST-1: `tags.choose` MUST accept an optional `with.ai` block (Interfaces). Without it,
  the step's reviews offer no AI suggestions and no Ask AI button. With it, the workflow sets
  *what* is sent: `context` (album fields), optional `guidance` (a Markdown file, AI-CORE-12),
  optional `provider`, `max_suggestions` (1 to 10, default 3), `allow_new` (default true), and
  optional `mode` (AI-SUGGEST-7): the workflow's default for when the AI runs automatically.
- AI-SUGGEST-2: The request MUST contain only: the task's prompt template; the `guidance` text;
  the choice's message and its `show` fields with their values; the `rank_by` values; every
  option the review offers (label, and the rule's `description` if the rules file has one,
  AI-SUGGEST-3) with its rules rank; and, when `options_from` is set, the labels of all rules
  in the file, including rules that didn't match; the `custom` field names; and the `context`
  values. The task's `sends` lists these.
- AI-SUGGEST-3: A rule in a choice rules file MAY have a `description` (plain text, at most
  500 characters). It is shown in review next to the option and sent to the AI. It has no
  other effect. `learn: true` writes rules without one.

### The answer
- AI-SUGGEST-4: The `choice.suggest` task's answer is a ranked list of up to `max_suggestions`
  suggestions, each with `values` (a value for each `custom` field the suggestion sets, or the
  label of a known option), `confidence` (0 to 1) and `rationale` (at most 500 characters).
- AI-SUGGEST-5: Each suggested value MUST be matched with AI-CORE-13 against the option labels
  and the rules file's labels (`open` mode when `allow_new` is true, `strict` otherwise):
  - a match to an option the review offers becomes a suggestion **of that option**, so
    choosing it runs the option's actions;
  - a match to a rule label that isn't offered (its query didn't match) becomes a suggestion of
    that rule's actions, run on the album's current values like any option;
  - a new value (only with `allow_new`) becomes a **custom** suggestion, marked "New" in
    review, with the lookalike map's preferred characters (AI-CORE-13). Its values MUST pass
    `backend.coerce` and `backend.check` for the `custom` fields; a value that fails is dropped
    (`invalid_value`). A step without `custom` fields MUST NOT get custom suggestions.
  Suggestions that end up the same after matching are merged, keeping the first rank.
- AI-SUGGEST-6: Choosing an AI suggestion MUST be an ordinary decision (TAGS-CHOOSE): a known
  option is `choose` with that option; a new value is `choose` with `option: custom` and the
  values. So `learn: true` adds a rule for a chosen new value exactly as it does for a typed
  one, with the preferred characters. The applied ChangeSet's rationale MUST say the choice
  was an AI suggestion (provider and model). AI suggestions MUST NOT count as options for
  `when_single: auto` and MUST NOT be applied without the person's decision (non-negotiable 4).

### When it runs
- AI-SUGGEST-7: Each step's suggestion mode is `off`, `unsure` or `always`, taken from the
  first of these that is set (author, 2026-11-26): the Settings page's override for that
  workflow step (F-SETTINGS), then the step's `with.ai.mode`, then the profile's
  `ai.suggest.mode` (default `unsure`). `off` means only the buttons run it. `unsure` runs it automatically when any of these
  holds: the review offers no option; every option it offers is a fallback option; the top two
  ranked rule options have the same rank. `always` runs it for every review of the step.
- AI-SUGGEST-8: An automatic run MUST happen when the review is created, before the
  `review.needed` notification. It MUST NOT hold the lane longer than `ai.timeout` plus
  retries. If it fails, the review opens without AI suggestions, with a note on the card
  ("AI suggestion failed: <reason>") and a run warning. The lane never fails and never waits
  on the AI after that (non-negotiable 6). With no AI configured (AI-CORE-5), automatic runs
  are silently off and the buttons are hidden.
- AI-SUGGEST-9: Suggestions MUST be stored with the choice review (provider, model, prompt
  version, usage, and the time) and emitted as `choice.ai_suggested` over SSE, so an open
  queue shows them live. Asking again replaces them. Asking again with `refresh: true` skips
  the cache (AI-CORE-17).

### UI and routes
- AI-SUGGEST-10: `POST /api/choices/{id}/ai-suggest {refresh?}` MUST run the task for one
  waiting choice review and return when the suggestions are stored, or with the error. A
  review that isn't waiting, or whose step has no `with.ai`, MUST be a 400.
- AI-SUGGEST-11: `POST /api/choices/ai-suggest {ids? , workflow?, step_id?, refresh?}` MUST
  start an AI job (AI-CORE-19) over the waiting choice reviews it names, or over all waiting
  reviews of the step. Reviews that already have suggestions are skipped unless `refresh`. It
  returns the job id at once; progress arrives as `ai.job.progress` and each review's
  `choice.ai_suggested`.
- AI-SUGGEST-12: A choice card whose step has `with.ai` MUST show an **Ask AI** button (or
  **Ask again** once it has suggestions) and a "Show what will be sent" link to the preview
  (AI-CORE-18, `input` = `{choice_id}`). While a request runs, the button shows progress and
  the card stays usable: the person can decide without waiting.
- AI-SUGGEST-13: AI suggestions MUST show as their own ranked section under the rules options:
  label or values, confidence, rationale, and a "New" badge for a custom suggestion. Each one
  is one click to choose. The rules' own preselection stays as it is (S-TAGS-CHOOSE). An AI
  suggestion is never preselected.
- AI-SUGGEST-14: The review queue MUST show **Suggest with AI for all** at the top when at
  least one waiting choice review has `with.ai` and no suggestions. Clicking it MUST show the
  count of reviews and the estimated tokens (and cost when known, AI-CORE-19) and ask for
  confirmation, then start AI-SUGGEST-11 for those reviews. It shows live progress with a
  cancel button, and cards fill in as their suggestions arrive.

## Interfaces
```yaml
# workflow step (foobar-style genre-bucket)
- id: genre-bucket
  uses: tags.choose
  with:
    show: [subgenres, genres]
    options_from: genres.yaml
    rank_by: subgenres
    custom: [genres]
    learn: true
    ai:
      context: [albumartist, album, year, label, country, albumtype, subgenres]
      guidance: rules/genre-guidance.md   # optional, the user's notes for the AI
      max_suggestions: 3
      allow_new: true
      # mode: always                      # optional workflow default; Settings can override
      # provider: local                   # optional; default is the profile's default
```
```yaml
# rules/genres.yaml: a rule may describe itself (AI-SUGGEST-3)
rules:
  - label: "+Cross-Genre Collabs"
    description: A collaboration or split whose artists usually come from two or more buckets.
    actions: [{ set: { genres: ["+Cross-Genre Collabs"] } }]
```
```yaml
# profile.yaml (edited on the Settings page, F-SETTINGS)
ai:
  suggest:
    mode: unsure                  # off | unsure | always; used when nothing else is set
    steps:                        # optional overrides, "<workflow>/<step id>"; win over the
                                  # step's own `with.ai.mode"
      foobar-style/genre-bucket: always
```
```python
class ChoiceAIParams(BaseModel):
    context: list[str] = []
    guidance: str | None = None
    provider: str | None = None
    max_suggestions: int = Field(3, ge=1, le=10)
    allow_new: bool = True
    mode: Literal["off", "unsure", "always"] | None = None

class AISuggestion(BaseModel, frozen=True):
    rank: int
    option: str | None             # an offered option's id, or a rule label; None for new
    values: dict[str, list[str]]   # custom field -> values, for a new suggestion
    new: bool
    confidence: float
    rationale: str
```
```jsonc
// output schema of the choice.suggest task
{"suggestions": [{"values": {"genres": ["Alt／Indie"]}, "confidence": 0.7, "rationale": "…"}]}
```
- `ChoiceReview` gains `ai: {suggestions: list[AISuggestion], provider, model,
  prompt_version, usage, at, error: str | None} | None` (additive API change; TS client
  regenerated).
- Routes: `POST /api/choices/{id}/ai-suggest`, `POST /api/choices/ai-suggest`.
- Event: `choice.ai_suggested {choice_id, count, error?}`.
- Modules: `rifferr.ai.tasks.choice_suggest`; `rifferr.steps.tags_choose` (auto mode);
  frontend: the choice card and the queue header.

## Examples
Profile with the genre buckets and the lookalike map (AI-CORE-22). Album *Punk Junkies* by GBH
with no `subgenres`: the review offers no rule option, so `unsure` runs the AI when the review
opens. The model answers `["Punk", "Hardcore", "UK82/Street Punk"]`:
- `Punk` and `Hardcore` match rule labels → suggestions 1 and 2, each running that rule's
  actions.
- `UK82/Street Punk` is new → suggestion 3, offered as `UK82／Street Punk` with a "New" badge.
  Choosing it sets `genres: [UK82／Street Punk]`, and `learn: true` adds that rule.

An album whose sub-genres match Shoegaze and Alt／Indie once each (a tie) → `unsure` runs.
An album with three Punk-matching sub-genres and one Rock → no tie, no automatic run; the
Ask AI button is still there.

The model answers `alt/indie` → suggestion of the existing `Alt／Indie` option, never a new
`alt/indie`.

## Extensibility
- Any `tags.choose` step can use it. What is sent is the workflow's choice. When it runs is
  the workflow's default, and the user's Settings can override it.
- The task is a normal `AITask`, so a plugin could register a different suggest task. Selecting
  it per step is left for later.
- Fixed: suggestions never apply on their own, never preselect, and go through the
  TAGS-CHOOSE decision path.

## Security & safety
- Same as C-AI-CORE: only the listed data is sent, hosts come from the profile, keys stay in
  secrets.
- The bulk button always confirms, with the count and estimated tokens, before sending
  anything.
- A new value is still checked by `backend.check`, and still needs the person to choose it.

## Non-goals
- AI suggestions on review gates or cover reviews (later tasks on C-AI-CORE).
- Library-aware context such as the credited artists' usual buckets (B-095). That needs an
  album fact first, which could then be listed in `context`.
- Fetching genres from MusicBrainz or Discogs (F-GENRE-FETCH).

## Open questions
1. ~~Should an AI top pick ever replace the rules' preselection?~~ Resolved 2026-11-26
   (author): never. An AI suggestion is never preselected (AI-SUGGEST-13).
2. ~~May a workflow set a default mode for its step?~~ Resolved 2026-11-26 (author): yes,
   `with.ai.mode`. A Settings override for the step wins over it (AI-SUGGEST-7).
3. Should a bulk job put several albums in one request to save tokens? This draft: one per
   album, for caching and per-album errors.
4. Should `unsure` also count "the top option's rank is low" (e.g. one sub-genre out of five
   matched)? This draft keeps the three clear cases.

## Tasks
- [ ] `with.ai` params and validation; `description` on choice rules (AI-SUGGEST-1 to -3)
- [ ] `choice.suggest` task: prompt, schema, matching (AI-SUGGEST-4, -5)
- [ ] Decisions on AI suggestions; rationale; `when_single` exclusion (AI-SUGGEST-6)
- [ ] Mode from settings; automatic run at review creation; failure note (AI-SUGGEST-7, -8)
- [ ] Storage, event, routes (AI-SUGGEST-9 to -11)
- [ ] Choice card: Ask AI, preview link, suggestions section; queue header bulk button with
      confirmation and progress; Playwright tests for both (AI-SUGGEST-12 to -14)
- [ ] foobar-style `genre-bucket` gets `with.ai`; `examples/rules/genres.yaml` descriptions;
      `examples/rules/genre-guidance.md`; `docs/coverage.md` row 14
