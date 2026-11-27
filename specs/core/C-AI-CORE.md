# C-AI-CORE: Shared AI core: providers, tasks, validation, cache
Status: Implemented
Milestone: M4

## Context
Architecture §7. Several features need a language model: the `ai.tag` step (S-AI-TAG), AI
suggestions on a choice review (F-AI-SUGGEST), and later the chat panel (F-AI-CHAT), cover
ranking, advisory workflow safety review (SECURITY-12) and library-aware suggestions (B-095).
They all need the same things: providers, keys, a host allowlist, retries, a cache, usage
reporting, a preview of what is sent, and validation of the answer before anyone sees it.

This spec is that shared layer. Each feature is an **AI task**: a plugin that says what it
sends, what answer shape it expects and how the answer is validated. The core runs every task
the same way, so a new feature can't skip the safety rules, and a third-party plugin gets the
same guarantees as a built-in (non-negotiable 7).

The AI only answers. What a feature does with a validated answer (a ChangeSet proposal, a
suggestion on a review) belongs to that feature, and nothing the AI returns is applied without
a person (non-negotiable 4).

Split from S-AI-TAG's 2026-11-18 draft (author, 2026-11-26): the provider, profile, HTTP,
retry, cache, preview and safety-report behaviors moved here.

## Behavior

### Providers
- AI-CORE-1: An AI provider MUST implement the `AIProvider` protocol (Interfaces) and be
  registered under the entry point group `rifferr.ai_providers`. Built-ins use the same group.
- AI-CORE-2: Rifferr MUST ship two providers: `anthropic` (Messages API, structured output
  through a forced tool call) and `openai-compatible` (Chat Completions with a JSON-schema
  response format, falling back to a forced function call when the server reports that
  `response_format` is unsupported). `openai-compatible` takes a `base_url`, which covers
  OpenAI, Ollama, LM Studio and OpenRouter. Both call the APIs through the existing `httpx`
  dependency; no vendor SDK is added.
- AI-CORE-3: A provider MUST receive an `AIRequest` (system text, user text, output JSON
  Schema, `max_output_tokens`, `temperature`) and return an `AIResponse` holding the parsed
  JSON object, the raw text, the model name the server reports, and `usage` (input and output
  tokens, and cost when the provider reports it, each `None` when unknown). A provider MUST NOT
  retry by itself (AI-CORE-14).
- AI-CORE-4: Providers MUST make HTTP requests only through the `AIHttp` client the core
  passes in. It MUST allow only the host of the configured `base_url` (or the provider's fixed
  default host), MUST use the profile's `ai.timeout`, MUST cap the response body at
  `ai.max_response_bytes`, and MUST NOT follow redirects to another host. `http://` is allowed
  only for loopback and private-network hosts (local model servers). Every other host MUST use
  `https://`.

### Profile settings
- AI-CORE-5: The profile's `ai` section (Interfaces) MUST list the configured providers, each
  with `name` (unique in the profile), `type` (the entry point name), `model`, optional
  `base_url`, optional `api_key` (a secret name from `secrets`, PROFILE-4), `enabled`
  (default true) and `settings`. The first enabled provider is the profile's default. Missing
  `ai`, or no enabled provider, MUST make every AI task report "AI is not configured for this
  profile": a step that needs AI fails validation; a feature that only offers AI (a button)
  hides or disables that offer.
- AI-CORE-6: An API key MUST come only from the profile's `secrets` (keyring, environment or
  file). A literal key anywhere in `profile.yaml` or a workflow MUST be a validation error
  that doesn't repeat the value. The key is resolved at the point of use (PROFILE-5) and MUST
  NOT appear in events, logs, run records, cache rows, previews or error messages, even when
  a provider echoes it back.
- AI-CORE-7: A workflow or task input MUST NOT be able to set `base_url`, `api_key` or
  `type`. It MAY name one of the profile's providers. Naming a provider the profile doesn't
  have, or one that is disabled, MUST be a validation error (or, at run time, a task error).

### Tasks
- AI-CORE-8: An AI feature MUST be an `AITask` (Interfaces) registered under the entry point
  group `rifferr.ai_tasks`. Built-in tasks (`tag`, `choice.suggest`) use the same group. A
  task has an `id`, a `prompt_version`, an output JSON Schema, a `build` function that turns
  the task input into system and user text, a pure `validate` function, and `sends`: the list
  of data kinds it puts into a request (field names, rules files, vocabularies).
- AI-CORE-9: The core MUST run every task through the same path: resolve the provider
  (AI-CORE-7), look up the cache (AI-CORE-16), build the request, call the provider, validate
  the answer against the task's output schema (AI-CORE-10), then call the task's `validate`,
  and emit the events in AI-CORE-15. A task MUST NOT call a provider or `AIHttp` itself.
- AI-CORE-10: An answer that doesn't parse, or doesn't validate against the task's output
  schema, MUST count as a failed attempt (AI-CORE-14). The core MUST NOT pass an unvalidated
  answer to the task's `validate` or to any caller.
- AI-CORE-11: A task's `build` MUST receive only its own declared input (values the calling
  feature collected, rules and vocabulary text the core loaded). It MUST NOT receive the
  backend, the filesystem, the profile or secrets. Every value sent MUST appear in the preview
  (AI-CORE-18).
- AI-CORE-12: Rules and vocabulary files named by a task input MUST be paths relative to the
  profile directory that resolve inside it (no `..` escape, no symlink out), UTF-8, at most
  `ai.max_rules_bytes` (default 64 KiB) each. The core reads them. A missing or too large file
  MUST fail the task before any request is made.

### Vocabulary matching
- AI-CORE-13: The core MUST provide pure vocabulary matching (`rifferr.core.ai_validate`) for
  tasks to use. Two values match when their *match keys* are equal. The match key is the value
  after Unicode NFKC normalization, then the profile's **lookalike map** (AI-CORE-22) applied
  in reverse (every variant becomes its group's preferred character), then case folding, then
  stripping and collapsing whitespace. A matched value MUST be replaced by the vocabulary's own
  spelling, so the user's characters (e.g. `／`) are kept. Each use picks a mode:
  - `strict`: a value with no match is dropped (reason `not_in_vocabulary`);
  - `flag`: it is kept with the warning `not_in_vocabulary`;
  - `open`: it is kept and marked `new`.
  A value kept by `flag` or `open` MUST have each lookalike variant rewritten to its group's
  preferred character, so a new value the model writes as `Rock/Pop` is offered as `Rock／Pop`
  when the map prefers `／`. A vocabulary with two entries that share a match key MUST fail the
  task with an error that names both entries. Duplicates in one answer, after matching, MUST be
  removed, keeping the first.
- AI-CORE-22: The lookalike map MUST be user data, not engine code (non-negotiable 1). It is
  a rules file named by `ai.lookalikes` in the profile (relative to the profile directory,
  same path rules as AI-CORE-12). The file lists groups, and each group has one `preferred`
  character and its `variants`. Without `ai.lookalikes`, only NFKC applies. A character listed
  in two groups, or a group whose preferred character is also one of its own variants, MUST be
  a validation error. The prompt builder MUST tell the model the preferred characters (e.g.
  "write `／` for a slash"), and the preview (AI-CORE-18) shows that line. The same file is
  meant to be shared with tag scripts that rewrite these characters (BACKLOG B-093), so new
  imports and AI answers agree.

### Retries, events, usage
- AI-CORE-14: A failed attempt (network error, timeout, HTTP 429 or 5xx, an invalid answer)
  MUST be retried up to the caller's `retries` (default 2) with exponential backoff, honouring
  a `Retry-After` header up to `ai.max_retry_wait` (default 60 s). Other 4xx errors (bad key,
  unknown model) MUST NOT be retried. When the attempts run out, the task returns an error to
  its caller, which decides what happens (fail a lane, skip, show a message).
- AI-CORE-15: The core MUST emit `ai.request` (task, target, provider, model, attempt,
  `cached`), `ai.response` (task, target, usage, duration) and `ai.rejected` (task, target,
  field or value, reason) events. Values in `ai.rejected` MUST be cut to 200 characters.
  Every task result MUST carry its usage, so callers can sum `input_tokens`, `output_tokens`
  and `cost`. `cost` is the provider's reported cost when it reports one. Otherwise, when the
  provider's settings have the optional `price_per_mtok` (AI-CORE-23), it is computed from the
  token counts. Otherwise it is `None`.
- AI-CORE-23: A provider entry MAY set `settings.price_per_mtok: {input, output, currency}`:
  prices per million tokens, decimals, `currency` default `USD`. It is optional: without it,
  everything works and costs show as unknown, with token counts only. A negative price or a
  price without both `input` and `output` MUST be a validation error. A reported cost is always
  shown, but a computed one MUST be marked as an estimate.

### Cache
- AI-CORE-16: Answers MUST be cached in the app DB under the SHA-256 of: the task id and
  prompt version, the provider type and model, `temperature`, and the exact system and user
  text sent (which includes the rules, vocabularies and values). The cache stores the
  schema-validated answer (AI-CORE-10), before the task's `validate`, so a change that only
  affects the task's checks re-validates without a new request. Changing a prompt template
  MUST bump that task's prompt version.
- AI-CORE-17: A cache hit MUST NOT make a request, and MUST be reported with `cached: true`
  and zero usage. A caller MAY pass `refresh: true` to skip the lookup and overwrite the
  entry. Entries expire after `ai.cache_ttl` (default 90 days), and the cache MUST be capped by
  `ai.cache_max_entries` (default 50 000, least recently used dropped first). Failed attempts
  MUST NOT be cached.

### Preview and jobs
- AI-CORE-18: `POST /api/ai/preview {profile, task, input}` MUST return exactly what the task
  would send (system text, user text, output schema, provider name, model, host), whether the
  cache already has an answer, and an estimated input token count, without contacting the
  provider. Each feature defines how its UI builds `input` (e.g. a workflow step and an album).
  The route follows F-SECURITY's rules for mutating endpoints (JSON-only, Origin check) even
  though it changes nothing, because it reads rules files.
- AI-CORE-19: Running one task over many targets (a bulk action) MUST be an AI job: it runs
  targets concurrently up to `ai.concurrency` (default 2), emits `ai.job.progress` (done,
  total, failed, usage so far) and `ai.job.finished` over SSE, is cancellable (a cancel stops
  new requests and lets running ones finish or time out), and one target's failure MUST NOT
  stop the others. Before a bulk job starts from the UI, the UI MUST show the target count and
  the estimated input tokens from AI-CORE-18 (and cost, when the provider's settings give a
  price) and ask for confirmation.

### Safety report and testing
- AI-CORE-20: The static safety report (SECURITY-11) MUST list each workflow step that runs an
  AI task as an `ai_request` finding with the task, the provider name, the provider's host (or
  "profile default") and the task's `sends`. A path field (`path`, `artpath`) in what is sent
  MUST be a `path_sent` warning. AI tasks don't need workflow trust (they run no code), but the
  entry says that album data leaves the machine unless the host is local.
- AI-CORE-21: A `FakeAIProvider` (in `rifferr.ai.testing`) MUST return scripted answers and
  record each request, so engine, step and API tests run without network. The two built-in
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
      settings:
        price_per_mtok: {input: 1.00, output: 5.00, currency: USD}   # optional (AI-CORE-23)
    - name: local
      type: openai-compatible
      base_url: http://192.168.1.20:11434/v1
      model: qwen3:14b
      settings: {temperature: 0}
```
```yaml
# profile.yaml (continued)
ai:
  lookalikes: rules/lookalikes.yaml
```
```yaml
# rules/lookalikes.yaml (example: examples/rules/lookalikes.yaml, the B-093 lists)
groups:
  - preferred: "／"     # U+FF0F
    variants: ["/", "∕", "⁄", "⧸", "╱", "⟋"]
  - preferred: "꞉"      # U+A789
    variants: [":", "∶", "ː", "˸", "﹕", "：", "⁚", "፡", "⦂"]
  - preferred: "？"     # U+FF1F
    variants: ["?", "﹖", "ʔ"]
```
NFKC runs first and turns `／` into `/`, `：` into `:` and `？` into `?`. Then the map turns them
back into the preferred character, so both forms reach the same key. NFKC doesn't fold `꞉`,
`∕`, `⧸`, `ʔ` and the others, so the map lists them too.

Feature settings (e.g. F-AI-SUGGEST's `suggest` modes) live in their own keys under `ai:`
and are edited on the Settings page (F-SETTINGS).

```python
class AIUsage(BaseModel, frozen=True):
    input_tokens: int | None = None; output_tokens: int | None = None
    cost: Decimal | None = None; currency: str | None = None

class AIRequest(BaseModel, frozen=True):
    system: str; user: str; output_schema: dict[str, object]
    max_output_tokens: int = 4096; temperature: float = 0.0

class AIResponse(BaseModel, frozen=True):
    data: dict[str, object]; raw: str; model: str; usage: AIUsage

class AIProviderConfig(BaseModel, frozen=True):    # one profile entry, key not included
    name: str; type: str; model: str; base_url: str | None
    settings: Mapping[str, object]

class AIProvider(Protocol):
    type: str; default_host: str | None
    async def complete(self, request: AIRequest, config: AIProviderConfig,
                       api_key: Secret | None, http: AIHttp) -> AIResponse: ...

class AITaskInput(BaseModel, frozen=True):         # each task subclasses this
    rules_files: Mapping[str, str] = {}            # name -> path in the profile directory

class AITask(Protocol[In, Out]):
    id: str                                        # "tag", "choice.suggest"
    prompt_version: int
    input_model: type[In]
    output_schema: dict[str, object]
    def sends(self, inp: In) -> list[str]: ...     # for the safety report and preview
    def build(self, inp: In, files: Mapping[str, str]) -> tuple[str, str]: ...  # system, user
    def validate(self, answer: dict[str, object], inp: In) -> AITaskResult[Out]: ...

class AITaskResult(BaseModel, Generic[Out], frozen=True):
    value: Out; rejected: list[Rejection]; usage: AIUsage; cached: bool

# the one entry point features call
async def run_task(task: AITask[In, Out], inp: In, *, provider: str | None,
                   retries: int = 2, refresh: bool = False) -> AITaskResult[Out]: ...

class VocabMatch(BaseModel, frozen=True):
    value: str; known: bool; warnings: list[str] = []
def match_vocabulary(values: Sequence[str], vocabulary: Sequence[str],
                     mode: Literal["strict", "flag", "open"]
                     ) -> tuple[list[VocabMatch], list[Rejection]]: ...
```
As implemented (2026-11-26; `rifferr.ai`, `rifferr.core.ai`, `rifferr.core.ai_validate`):
- A task's answer shape is a Pydantic model, `output_model`; the JSON Schema sent to the model
  is `output_model.model_json_schema()`, and AI-CORE-10's check is `model_validate`. This needs
  no JSON Schema library. Protocol members (`id`, `prompt_version`, `type`, …) are read-only
  properties, so plain class attributes satisfy them.
- `AIRuntime` (`rifferr.ai.runner`) holds one profile's settings, providers, cache, secret
  lookup and event sink; `run_task(task, input, provider=, retries=, refresh=, target=)` and
  `preview(...)` are its methods. Failures raise `AITaskError`.
- The server's `AIService` (`ServerState.ai`) builds runtimes and holds `AIJobs`. Its events
  (`ai.request`, `ai.response`, `ai.rejected`, `ai.job.*`) go on the profile's stream;
  `ai.job.*` also reaches `GET /api/events`. Putting `ai.*` events on a run's stream, for a
  task run by a workflow step, comes with the first step that runs one (S-AI-TAG).
- The confirmation before a bulk job (AI-CORE-19, last sentence) is UI, built with the first
  bulk button (F-AI-SUGGEST AI-SUGGEST-14). `AIPreview` gives the numbers:
  `estimated_input_tokens` (text length / 4) and `estimated_cost_max` (input estimate plus
  `max_output_tokens`, when priced).
- `http://` is accepted for loopback, private, link-local and 100.64.0.0/10 addresses, and for
  `localhost`, single-label names and `.local`, `.lan`, `.home.arpa`, `.internal`, `.ts.net`
  names.
- The cache is per profile (`ai_cache` keyed by profile and key), so Open question 3 is
  answered "per profile".
- Routes: `POST /api/ai/preview`, `GET /api/ai/jobs/{id}`, `POST /api/ai/jobs/{id}/cancel`.

- App DB (migration): `ai_cache` (key, task, provider_type, model, prompt_version, answer
  JSON, created_at, last_used_at).
- Events: `ai.request`, `ai.response`, `ai.rejected`, `ai.job.progress`, `ai.job.finished`.
- Routes: `POST /api/ai/preview`; `POST /api/ai/jobs/{id}/cancel`. Bulk jobs are started by
  each feature's own route.
- Modules: `rifferr.ai` (protocols, `AIHttp`, cache, `run_task`, jobs), pure validation in
  `rifferr.core.ai_validate`, `rifferr.ai.providers.{anthropic,openai_compat}`,
  `rifferr.ai.testing`.

## Examples
With the lookalike map above and vocabulary `[Alt／Indie, Garage Rock／Punk, Hip-Hop]`:
- Answer `["Alt/Indie", "garage rock∕punk", "Folk/Pop"]`, mode `open` →
  `[Alt／Indie (known), Garage Rock／Punk (known), Folk／Pop (new)]`. The known ones take the
  vocabulary's spelling. The new one has its `/` rewritten to `／`.
- Same answer, mode `strict` → `[Alt／Indie, Garage Rock／Punk]`; `Folk/Pop` rejected
  `not_in_vocabulary`.
- `hip hop` doesn't match `Hip-Hop`: a hyphen isn't a lookalike of a space. Matching folds
  characters, it doesn't guess spellings. In `open` mode it shows as new.
- `Alt / Indie` (spaces around the slash) doesn't match `Alt／Indie`: whitespace is only
  collapsed, not removed.
- Without `ai.lookalikes`: `Alt/Indie` still matches `Alt／Indie`, because NFKC turns both
  into `alt/indie`, and the result is the vocabulary's `Alt／Indie`. But `Alt∕Indie` (U+2215)
  doesn't match, because NFKC doesn't fold it, and a new value keeps the model's `/`.
- The same task, input and model run twice → the second `ai.request` has `cached: true` and
  no HTTP request is made.
- `api_key: sk-…` in `profile.yaml` → validation error that doesn't contain `sk-`.

## Extensibility
- New providers: an `AIProvider` under `rifferr.ai_providers`.
- New AI features: an `AITask` under `rifferr.ai_tasks`. A task gets the providers, retries,
  cache, events, preview, jobs and vocabulary matching for free, and can't skip any of them.
- Fixed: the run path in AI-CORE-9, schema validation before the task sees the answer, the host
  allowlist, secret handling, and that a task never applies anything itself.

## Security & safety
- Tag values and rules files are data, not instructions to Rifferr. A tag that tries to steer
  the model can at worst change the answer, which is still validated by the task and waits for
  a person.
- Album data leaves the machine for remote providers. The safety report and the preview say
  exactly what goes where. Hosts come only from the profile.
- Keys stay in secrets and are masked everywhere (AI-CORE-6).
- Files are confined to the profile directory (AI-CORE-12).

## Non-goals
- The `ai.tag` step (S-AI-TAG), choice suggestions (F-AI-SUGGEST), the chat panel (F-AI-CHAT).
- Model tool calls, web browsing or MusicBrainz lookups by the model inside a task.
- Audio analysis, embeddings, fine-tuning.

## Open questions
1. Match keys: should the lookalike map also be able to treat `-` and space as the same, so
   `hip hop` matches `Hip-Hop`? This draft keeps the map to single characters, so results are
   predictable. The `open` mode shows such values as new instead.
2. ~~Cost: should `settings` take a per-model price?~~ Resolved 2026-11-26 (author): yes, as
   an optional `price_per_mtok` (AI-CORE-23).
3. Should the cache be shared between profiles that use the same provider and model? This draft
   keeps it per profile (the app DB is per profile).

## Tasks
- [x] `AIProvider`, `AITask` protocols, entry point groups, `AIHttp` (AI-CORE-1, -3, -4, -8)
- [x] Profile `ai` section and validation; secrets (AI-CORE-5 to -7)
- [x] `anthropic` and `openai-compatible` providers with respx tests (AI-CORE-2, -21)
- [x] `FakeAIProvider` (AI-CORE-21)
- [x] `run_task`: schema validation, file loading, retries, events (AI-CORE-9 to -12, -14, -15)
- [x] `core.ai_validate.match_vocabulary` with Hypothesis tests (idempotence; fullwidth and
      lookalike cases) (AI-CORE-13)
- [x] `ai.lookalikes` file, validation, prompt line; `examples/rules/lookalikes.yaml` with the
      B-093 lists (AI-CORE-22)
- [x] Cache table, TTL, cap (AI-CORE-16, -17)
- [x] Preview route; AI jobs with SSE progress and cancel (AI-CORE-18, -19)
- [x] Safety report findings (AI-CORE-20)
- [x] Optional `price_per_mtok`; reported vs estimated cost (AI-CORE-15, -23)
