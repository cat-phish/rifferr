# S-AI-TAG: `ai.tag` step, AI providers, validation and cache
Status: Draft
Milestone: M4

## Context
Architecture §7. Some tagging needs judgment that a rule can't capture well: sorting
free-form genres into a user's own broad categories, for example (reference checklist line 14,
"resolve genre fields into broad categories"). `ai.tag` asks a language model to propose
values for a fixed list of fields and turns the answer into ordinary ChangeSets
(C-CHANGESET), which wait in the review queue (S-REVIEW, F-REVIEW-QUEUE) like any other
proposal.

The model never writes anything (AGENTS.md non-negotiable 4). Its answer is schema-validated,
limited to the step's field allowlist, checked against the step's vocabularies, and then
proposed through `ctx.propose`. Which fields, which vocabulary, which rules and which provider
are all the user's choice: nothing about genres or any other field is built in
(non-negotiable 1). Copying `genre` to a sub-genre field is a `tags.script` job, not this
step's.

This spec covers the provider plugin type, the profile's `ai` settings, the `ai.tag` step,
validation, caching, usage reporting and a preview of exactly what would be sent. The chat
panel is F-AI-CHAT.

## Behavior

### Providers
- AI-TAG-1: An AI provider MUST implement the `AIProvider` protocol (Interfaces) and be
  registered under the entry point group `rifferr.ai_providers` (non-negotiable 7). Built-ins
  use the same group.
- AI-TAG-2: Rifferr MUST ship two providers: `anthropic` (Messages API, structured output
  through a forced tool call) and `openai-compatible` (Chat Completions with a JSON-schema
  response format, falling back to a forced function call when the server reports that
  `response_format` is unsupported). `openai-compatible` takes a `base_url`, which covers
  OpenAI, Ollama, LM Studio and OpenRouter.
- AI-TAG-3: A provider MUST receive an `AIRequest` (system text, user text, output JSON
  Schema, `max_output_tokens`, `temperature`) and return an `AIResponse` holding the parsed
  JSON object, the raw text, the model name the server reports, and `usage` (input and output
  tokens, and cost when the provider reports it, each `None` when unknown). A provider MUST NOT
  retry by itself. Retries belong to the step (AI-TAG-17).
- AI-TAG-4: Providers MUST make HTTP requests only through the `AIHttp` client the step
  passes in. It MUST allow only the host of the configured `base_url` (or the provider's
  fixed default host), MUST use the profile's `ai.timeout`, MUST cap the response body at
  `ai.max_response_bytes`, and MUST NOT follow redirects to another host. `http://` is allowed
  only for loopback and private-network hosts (local model servers). Every other host MUST use
  `https://`.

### Profile settings
- AI-TAG-5: The profile's `ai` section (Interfaces) MUST list the configured providers, each
  with `name` (unique in the profile), `type` (the entry point name), `model`, optional
  `base_url`, optional `api_key` (a secret name from `secrets`, PROFILE-4) and `settings`.
  Missing `ai`, or an empty `providers` list, MUST make any `ai.tag` step a validation error
  that says AI is not configured for the profile.
- AI-TAG-6: An API key MUST come only from the profile's `secrets` (keyring, environment or
  file). A literal key anywhere in `profile.yaml` or a workflow MUST be a validation error
  that doesn't repeat the value. The key is resolved at the point of use (PROFILE-5) and MUST
  NOT appear in events, logs, run records, cache rows, previews or error messages, even when
  a provider echoes it back.
- AI-TAG-7: The workflow MUST NOT be able to set `base_url`, `api_key` or `type`. A step MAY
  name one of the profile's providers with `with.provider`. Otherwise the profile's first
  enabled provider is used. Naming a provider the profile doesn't have, or one that is
  disabled, MUST be a validation error.

### The `ai.tag` step
- AI-TAG-8: `ai.tag` MUST declare `force_review = True` (CHANGESET-6): `apply: auto` on it is a
  validation error, and `apply` defaults to `review`. Its ChangeSets MUST have
  `source.kind = ai` and `source.name = "ai.tag"` (CHANGESET-1).
- AI-TAG-9: `with.fields` MUST be a non-empty list of field names, the allowlist. Each field
  MUST be one the backend knows or a flexible attribute name that is valid for the backend
  (validated with `backend.check` at run time and by name shape at validation time).
  `with.scope` is `album` (default) or `item`. With `album`, proposals target the album;
  with `item`, they target items, and the model sees and answers for every track of the album
  in one request.
- AI-TAG-10: The step MUST make at most one model request per album and lane (plus retries,
  AI-TAG-17), and MUST run albums concurrently up to the profile's `ai.concurrency`
  (default 2). Albums whose lanes are waiting or failed are not affected by another album's
  request failing (C-LANES).
- AI-TAG-11: `with.when` MAY be a beets query (as in other steps). Albums it doesn't match are
  skipped without a request. `with.only_empty: true` MUST skip an album whose allowlisted
  fields already all have a non-empty value.
- AI-TAG-12: The request MUST contain only: the prompt template for the current prompt version
  (a constant in code, AI-TAG-21), the text of `with.rules` (if any), each allowlisted field's
  vocabulary (if any), the field allowlist with each field's current values, and the values of
  the fields listed in `with.context` for the album (and, with `scope: item` or when
  `with.item_context` is set, for each item). Nothing else from the snapshot, the filesystem
  or the profile MUST be sent. A path field (`path`, `artpath`) in `context` MUST produce a
  safety-report finding (AI-TAG-24).
- AI-TAG-13: `with.rules` MUST be a path relative to the profile directory that resolves
  inside it (no `..` escape, no symlink out), to a UTF-8 Markdown file of at most
  `ai.max_rules_bytes` (default 64 KiB). It is read when the step starts. A missing or too
  large file MUST fail the step before any request is made.
- AI-TAG-14: `with.vocabulary` MUST map an allowlisted field to a vocabulary file (same path
  rules as AI-TAG-13): one value per line, blank lines and lines starting with `#` ignored,
  surrounding whitespace stripped. `with.denylist` has the same shape. A vocabulary for a
  field that isn't in `fields` MUST be a validation error.

### Validation of the answer
- AI-TAG-15: The model's answer MUST be validated against the output schema (Interfaces)
  before anything else. An answer that doesn't parse or doesn't validate counts as a failed
  attempt (AI-TAG-17). A target the step didn't ask about (an unknown item id) MUST be
  ignored and logged.
- AI-TAG-16: For each proposed field value, in this order:
  1. A field not in `fields` MUST be dropped and logged as `ai.rejected` with reason
     `field_not_allowed`.
  2. A value equal (case-insensitively, after stripping) to a denylist entry MUST be dropped
     with reason `denylisted`.
  3. When the field has a vocabulary, a value MUST match an entry case-insensitively after
     stripping, and MUST then be replaced by the vocabulary's own spelling. A value with no
     match MUST be handled by `with.on_unknown_value`: `drop` (default) drops it with reason
     `not_in_vocabulary`; `flag` keeps it and adds the warning `not_in_vocabulary` to the
     `FieldChange` (AI-TAG-19).
  4. A confidence below `with.min_confidence` (default 0) MUST be dropped with reason
     `low_confidence`.
  5. The value MUST be converted with `backend.coerce` and pass `backend.check` (a list only
     for a multi-value field, CHANGESET-2). A value that fails is dropped with reason
     `invalid_value`.
  For a multi-value field each element is checked on its own, and duplicates after
  normalization are removed, keeping the first. If every element is dropped, the field change
  is dropped. A value equal to the current value is a no-op (CHANGESET-4).
- AI-TAG-17: A failed attempt (network error, timeout, HTTP 429 or 5xx, an invalid answer)
  MUST be retried up to `with.retries` times (default 2) with exponential backoff, honouring
  a `Retry-After` header up to `ai.max_retry_wait` (default 60 s). Other 4xx errors (bad key,
  unknown model) MUST NOT be retried. When the attempts run out, the album MUST be handled by
  `with.on_error`: `fail` (default) fails that album's lane, `skip` logs a warning and goes on
  without proposing anything. A failure MUST NOT stop other albums.
- AI-TAG-18: When validation leaves at least one field change for an album, the step MUST
  propose them with one `ctx.propose` call for that album (CHANGESET-14). Otherwise it
  proposes nothing for it and logs why.

### ChangeSets, review and usage
- AI-TAG-19: `FieldChange` MUST gain two optional fields, `confidence` (0 to 1) and `warnings`
  (list of strings), and the published proposal schema change MUST be additive. `ai.tag` fills
  the per-field `rationale` and `confidence` from the answer, and the ChangeSet `rationale`
  with the provider name, model and prompt version. The review view MUST show each field's
  rationale, confidence and warnings; a field change with a warning MUST be shown unticked by
  default, so accepting the ChangeSet as a whole leaves it out unless the reviewer ticks it.
- AI-TAG-20: The step MUST emit `ai.request` (album, provider, model, attempt, `cached`),
  `ai.response` (album, usage, duration) and `ai.rejected` (album, field, value, reason) events.
  Values in `ai.rejected` MUST be cut to 200 characters. The step's outputs MUST include
  `requests`, `cached`, `proposed`, `rejected`, `input_tokens`, `output_tokens` and `cost`
  (sums over what the provider reported, `None` when nothing was reported). The run page MUST
  show the token totals and the cost when known.

### Cache
- AI-TAG-21: Answers MUST be cached in the app DB under the SHA-256 of: the prompt version,
  the provider type and model, `temperature`, the field allowlist and `scope`, the rules
  text, each vocabulary and denylist text, and the exact context values sent. The cache stores
  the validated answer from AI-TAG-15 (before AI-TAG-16, so a vocabulary change re-validates
  without a new request because the vocabulary text is in the key anyway). Changing the prompt
  template MUST bump the prompt version.
- AI-TAG-22: A cache hit MUST NOT make a request, and MUST be reported with `cached: true` and
  zero usage. `with.refresh: true` MUST skip the cache lookup and overwrite the entry.
  Entries expire after `ai.cache_ttl` (default 90 days), and the cache MUST be capped by
  `ai.cache_max_entries` (default 50 000, oldest dropped first). Failed attempts MUST NOT be
  cached.

### Preview (transparency)
- AI-TAG-23: `POST /api/ai/preview {profile, workflow, step_id, album_id}` MUST return exactly
  what the step would send for that album (system text, user text, output schema, provider
  name, model, host) and whether the cache already has an answer, without contacting the
  provider. The review view of an `ai.tag` ChangeSet MUST link to the same preview for its
  album. The route follows F-SECURITY's rules for mutating endpoints (JSON-only, Origin check)
  even though it changes nothing, because it reads rules files.

### Safety report and testing
- AI-TAG-24: The static safety report (SECURITY-11) MUST list each `ai.tag` step as an
  `ai_request` finding with its provider name, the provider's host (or "profile default"),
  and the `context` and `item_context` fields it sends. A path field in them MUST be a
  `path_sent` warning. `ai.tag` doesn't need workflow trust (it runs no code), but its
  report entry says that album data leaves the machine unless the host is local.
- AI-TAG-25: A `FakeAIProvider` (in `rifferr.ai.testing`) MUST return scripted answers and
  record each request, so engine and step tests run without network. The two built-in
  providers MUST have `respx` tests with recorded request and response bodies, including an
  error answer, a 429 with `Retry-After`, and a schema-invalid answer.

## Interfaces
```yaml
# profile.yaml
secrets:
  anthropic_key: keyring:rifferr/anthropic
ai:
  concurrency: 2
  timeout: 60s
  max_response_bytes: 1MB
  max_rules_bytes: 64KB
  max_retry_wait: 60s
  cache_ttl: 90d
  cache_max_entries: 50000
  providers:                       # order: the first enabled one is the default
    - name: claude
      type: anthropic
      model: claude-haiku-5-5
      api_key: anthropic_key       # a name from `secrets`, never the key itself
    - name: local
      type: openai-compatible
      base_url: http://127.0.0.1:11434/v1
      model: qwen3:14b
      enabled: false
      settings: {temperature: 0}
```
```yaml
# workflow step
- id: genres
  uses: ai.tag
  apply: review                    # the default; `auto` is a validation error
  with:
    provider: claude               # optional, one of the profile's providers
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
    on_error: fail                 # fail | skip
    refresh: false
```
```python
class AIUsage(BaseModel, frozen=True):
    input_tokens: int | None = None; output_tokens: int | None = None
    cost: Decimal | None = None; currency: str | None = None

class AIRequest(BaseModel, frozen=True):
    system: str; user: str; output_schema: dict[str, object]
    max_output_tokens: int = 4096; temperature: float = 0.0

class AIResponse(BaseModel, frozen=True):
    data: dict[str, object]; raw: str; model: str; usage: AIUsage

class AIProviderConfig(BaseModel, frozen=True):    # one profile entry, key already resolved
    name: str; type: str; model: str; base_url: str | None
    settings: Mapping[str, object]

class AIProvider(Protocol):
    type: str; default_host: str | None
    async def complete(self, request: AIRequest, config: AIProviderConfig,
                       api_key: Secret | None, http: AIHttp) -> AIResponse: ...
```
```jsonc
// output schema sent to the model (scope: album). With scope: item, "targets" is keyed
// by the item ids the request listed.
{
  "targets": [{
    "id": "album",                         // or an item id
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
- App DB (migration): `ai_cache` (key, provider_type, model, prompt_version, answer JSON,
  created_at, last_used_at).
- Events: `ai.request`, `ai.response`, `ai.rejected`. Route: `POST /api/ai/preview`.
- Modules: `rifferr.ai` (protocol, HTTP client, cache, prompt, validation; validation is pure
  and lives in `rifferr.core.ai_validate`), `rifferr.ai.providers.{anthropic,openai_compat}`,
  `rifferr.steps.ai_tag`.

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
  `not_in_vocabulary`; the field change starts unticked in review.
- Same album, same rules and vocabulary, run again → `ai.request` with `cached: true`, no
  HTTP request.
- `apply: auto` on the step → validation error "ai.tag always goes to review".
- `api_key: sk-…` in `profile.yaml` → validation error that doesn't contain `sk-`.

## Extensibility
- New providers: an `AIProvider` under `rifferr.ai_providers`. They get the same HTTP client,
  retries, cache and validation; they can't skip validation or the review gate.
- Everything domain-specific is the user's: fields, context, rules text, vocabularies,
  denylists, thresholds, provider and model.
- Fixed: the output schema, the validation order (AI-TAG-16), `force_review`, and which data
  may be sent (AI-TAG-12).

## Security & safety
- Tag values and rules files are data, not instructions to Rifferr. A tag that tries to steer
  the model ("ignore the rules") can at worst change the proposal, which is still limited to
  the allowlist and vocabulary and waits for a human.
- Album data leaves the machine for remote providers; the safety report and the preview say
  exactly what goes where. Hosts come only from the profile, so a workflow can't send data to
  a host the user didn't configure.
- Keys stay in secrets (PROFILE-4, -5) and are masked everywhere (AI-TAG-6).
- Rules and vocabulary paths are confined to the profile directory (AI-TAG-13).

## Non-goals
- Chat panel and its tools (F-AI-CHAT).
- Auto-applying AI proposals.
- Letting the model call tools, browse the web or look up MusicBrainz during `ai.tag`.
- Audio analysis, embeddings, fine-tuning.
- An AI review of workflow safety (later; advisory only, SECURITY-12).

## Open questions
1. Non-negotiable 4 says AI output "follows the step's `apply` policy, which defaults to
   `review`", which reads as if `apply: auto` is allowed; C-CHANGESET's Security section says
   AI output can never auto-apply. This draft follows C-CHANGESET (`force_review = True`).
   Which is intended?
2. `on_error` default: `fail` (lane fails, visible on the dashboard) or `skip` (run succeeds,
   warning only)? Unattended runs may prefer `skip` with a notification.
3. Should a flagged (`not_in_vocabulary`) value offer "add to vocabulary" in review, which
   edits the vocabulary file (needs confirmation, like F-AI-CHAT's rule appends)? Or leave
   that to F-AI-CHAT?
4. Is one request per album the right batching? Several small albums per request would cost
   less on remote providers but makes caching and per-album failure coarser.
5. Should the cost estimate be shown before a manual run starts (token count from the
   preview), or only reported afterwards?
6. Dependencies: is it fine to call both APIs with the existing `httpx` (no `anthropic` or
   `openai` SDK)? This draft assumes yes, so no new dependency.

## Tasks
- [ ] `AIProvider` protocol, entry point group, `AIHttp` client (AI-TAG-1, -3, -4)
- [ ] Profile `ai` section and validation (AI-TAG-5 to -7)
- [ ] `anthropic` and `openai-compatible` providers with respx tests (AI-TAG-2, -25)
- [ ] `FakeAIProvider` (AI-TAG-25)
- [ ] Prompt builder, rules/vocabulary loading (AI-TAG-12 to -14)
- [ ] Pure validation in `core.ai_validate` with Hypothesis tests (AI-TAG-15, -16)
- [ ] `ai.tag` step: lanes, concurrency, retries, proposals (AI-TAG-8 to -11, -17, -18)
- [ ] `FieldChange.confidence` / `warnings`, schema and TS client regenerated (AI-TAG-19)
- [ ] Events and outputs, run page usage (AI-TAG-20)
- [ ] Cache table, TTL, cap (AI-TAG-21, -22)
- [ ] Preview route and review-view link, Playwright test (AI-TAG-23)
- [ ] Safety report findings (AI-TAG-24)
- [ ] Genre step in `examples/workflows/foobar-style.yaml` with example rules and vocabulary;
      `docs/coverage.md` row 14
