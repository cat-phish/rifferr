# F-SETTINGS: Settings page, AI section first
Status: Implemented
Milestone: M4

## Context
Profile settings live in `profile.yaml` (C-PROFILE), edited by hand today. The author wants
the AI integration "ultimately user configurable in the settings" (2026-11-26): which providers
to use, and when AI suggestions run (F-AI-SUGGEST). This spec adds a per-profile Settings page.
It starts with an AI section and is built so later sections (covers, notifications, triggers)
can be added.

Settings are user data, so the page edits `profile.yaml` itself, round-tripped with ruamel.yaml
so comments and order survive (CLAUDE.md conventions). Secrets stay in the keyring or
environment (non-negotiable 5).

## Behavior
- SETTINGS-1: The UI MUST have a Settings page per profile, reached from the main navigation.
  Its sections come from a registry, so a later section is one more entry. M4 ships the AI
  section only.
- SETTINGS-2: `GET /api/profiles/{p}/settings/{section}` MUST return the section's current
  values and a JSON Schema for them. `PUT` with the whole section MUST validate it against the
  same model the profile loader uses (C-PROFILE, AI-CORE-5). Any error is a 400 that lists the
  fields at fault, and nothing is written.
- SETTINGS-3: A successful `PUT` MUST write only that section's keys in `profile.yaml` through
  ruamel.yaml round-trip, keeping comments, order and every other key unchanged. It MUST write
  to a temporary file and rename it into place. When the file changed on disk since the `GET`
  (its hash is sent back as `base_hash`), the `PUT` MUST be a 409 and write nothing.
- SETTINGS-4: After a write, the profile's worker and the server MUST use the new settings for
  runs that start afterwards. A run already going keeps the settings it started with. A run
  that resumes after a pause (a review gate, RUN-14) MUST use the settings saved at the time it
  resumes, so a fix such as a new key or host reaches runs that are waiting.
- SETTINGS-5: The AI section MUST show and edit: the providers (name, type from the installed
  `rifferr.ai_providers`, model, `base_url`, enabled, order; the first enabled one is the
  default; an optional price per million input and output tokens, AI-CORE-23, left empty by
  default), `concurrency`, `timeout`, `cache_ttl`, the lookalike file (`ai.lookalikes`,
  AI-CORE-22), and the suggestion mode `off | unsure | always` with per-step overrides
  (AI-SUGGEST-7). The override list offers the profile's workflow steps that have `with.ai`,
  and shows each step's own `with.ai.mode` when it has one, so it's clear what an override
  replaces.
- SETTINGS-6: A provider's API key MUST be write-only. The page shows whether a key is set,
  never its value. Entering a key MUST store it in the OS keyring under a secret name the
  page proposes (`rifferr/<profile>/<provider name>`). The page then adds the `secrets` entry
  and sets the provider's `api_key` to that name. Clearing a key removes the keyring entry.
  When no keyring is available (e.g. Docker), the page MUST say to set the key as an
  environment variable instead, and show the `secrets` line to use. The key MUST NOT be
  logged, echoed in a response, or written to `profile.yaml` (AI-CORE-6).
- SETTINGS-7: A **Test connection** button per provider MUST send one tiny fixed request
  through C-AI-CORE (no album data) and show the model the server reports, or the error with
  the key masked.
- SETTINGS-8: All settings routes are mutating endpoints under F-SECURITY: JSON-only, Origin
  and Host checks, signed-in session. Only the signed-in user of the instance may change
  them.
- SETTINGS-9: Each job of a run MUST record a hash of every settings section it uses, in its
  `run.started` or `run.resumed` event (`data.settings`, section id to hash). When a resumed
  job's hashes differ from the previous job's, the engine MUST emit `run.settings_changed`
  with the ids of the changed sections, and the run page MUST show it in the run log. The
  hash covers the loaded values, so an edit that keeps every value the same is not a change.
  It holds no secrets: keys stay in the keyring and only their secret names are in a section.
- SETTINGS-10: A save that would start sending album data to a new host MUST be confirmed, as
  trusting a workflow is. A new host is one that is not local (AI-CORE-20), is the host of an
  enabled provider in the new values, and is not the host of any enabled provider in the saved
  values. This covers changing a provider's `base_url`, adding or enabling a remote provider,
  and changing a provider's type to one with another default host. The `PUT` MUST answer 409
  `confirm_hosts`, listing each new host with the providers that use it, unless every new host
  is in the body's `confirm_hosts`. The page then shows those hosts and saves again only when
  the user confirms them. A host that is already in use, a local host, or a disabled provider
  needs no confirmation. Edits made directly in `profile.yaml` are not checked.

## Interfaces
```python
class SettingsSection(Protocol):
    id: str                                   # "ai"
    title: str
    model: type[BaseModel]                    # the same model the profile loader uses
    def read(self, profile: ProfileDoc) -> BaseModel: ...
    def write(self, profile: ProfileDoc, value: BaseModel) -> None: ...   # ruamel doc
```
- Routes: `GET|PUT /api/profiles/{p}/settings/{section}`,
  `PUT|DELETE /api/profiles/{p}/settings/ai/providers/{name}/key {key}`,
  `POST /api/profiles/{p}/settings/ai/providers/{name}/test`.
- UI states: loading, clean, edited (Save / Discard), saving, conflict (409: reload or
  overwrite after showing the other change), error per field.

## Examples
- A user switches `suggest.mode` from `unsure` to `always` and saves. `profile.yaml` keeps its
  comments, and the next genre-bucket review runs the AI for every album.
- Someone edits `profile.yaml` by hand while the page is open, and then Save → 409; the page
  offers to reload.
- A key is entered for `claude` → keyring `rifferr/default/claude`; `profile.yaml` gains
  `secrets: {claude: keyring:rifferr/default/claude}` and `api_key: claude`.

## Extensibility
- New sections register a `SettingsSection`. Plugins may add one for their own settings.
- Fixed: round-trip writes, write-only secrets, the 409 conflict rule.

## Security & safety
- Keys go straight to the keyring and are never returned.
- Changing settings can send album data to a new host. The page says so when a provider's
  host isn't local, as the safety report does (AI-CORE-20), and the server refuses the save
  until the user confirms each new host (SETTINGS-10).

## Non-goals
- Editing workflows (B-078) or rules files.
- Settings other than AI in M4.

## Open questions
1. ~~Should changing a provider's host or adding a remote provider ask for confirmation, like
   trusting a workflow? This draft only shows a notice.~~ Answered (author, 2026-11-27): yes,
   ask for confirmation (SETTINGS-10).
2. ~~Keyring name: `rifferr/<profile>/<provider>`, or let the user type a name?~~ Answered
   (author, 2026-11-27): automatic, as implemented (keyring service `rifferr`, entry
   `<profile>/<provider>`, secret name `ai-<provider>`); the user never types it.
3. ~~A run resumed after a review gate uses the settings saved since it started. Keep that, or
   store the settings with the run?~~ Answered (author, 2026-11-27): keep the saved settings,
   and note in the run log when they changed since the run's previous job (SETTINGS-4, -9).

### As implemented (2026-11-26)
- `rifferr.settings`: `SECTIONS` registry (one entry, `AISection`, key `ai`, model `AISettings`).
  `save_section` merges the value into the file's own YAML nodes: map keys are updated in place,
  and lists of named maps (`providers`) are matched by `name`, so comments stay with their
  entries. A default value is written only where the file already has that setting (author,
  2026-11-26), so hand-written lines such as `timeout: 60s  # NAS is slow` and their comments
  stay; defaults the file never had are not added. A setting cleared to empty is removed. The
  whole profile is revalidated before an atomic write.
- Routes under `/api/profiles/{profile}/settings`: `GET` lists sections. `GET|PUT /{section}`
  takes `{values, base_hash}`; it answers 400 `invalid_settings` with `details: [{field,
  message}]`, or 409 `conflict`. `PUT|DELETE /ai/providers/{name}/key` and
  `POST /ai/providers/{name}/test` handle keys and the connection test. DELETE takes an empty JSON
  body, because every mutating request is JSON-only.
- Keys: keyring service `rifferr`, entry `<profile>/<provider>`, secret name `ai-<provider>`.
  Without a keyring, the key route answers 409 `no_keyring`, giving the variable
  `RIFFERR_<PROFILE>_<PROVIDER>_KEY` and the `secrets` line to add. The page shows the same
  instructions.
- Test connection runs the internal `connection.test` task, which sends a fixed message with no
  album data. It is not an entry point, is never cached and has no retries. If the key shows up
  in a provider error, it is masked.
- SETTINGS-4: the worker now reads the saved profile for each run job (`WorkerAI` included). A
  run that pauses at a review gate and resumes later starts a new job, so it picks up settings
  saved in between. See open question 3.
- SETTINGS-9: `settings.section_hashes` hashes each `SECTIONS` entry's loaded values (sha256 of
  the model's JSON, 16 hex digits). The worker passes them and the previous job's hashes (from
  the run's latest `run.started`/`run.resumed` event) to `Engine.create_run`. The engine adds
  them to its start event and emits `run.settings_changed` `{sections: [...]}` right after
  `run.resumed`. A run with no stored hashes reports nothing. Only registered sections count;
  cover settings join once they have a section.

## Tasks
- [x] Section registry, GET/PUT with schema, ruamel write, 409 (SETTINGS-1 to -4)
- [x] AI section form (SETTINGS-5)
- [x] Write-only keys with keyring and env fallback (SETTINGS-6)
- [x] Test connection (SETTINGS-7)
- [x] Security checks and Playwright test (SETTINGS-8)
- [x] Settings hash per job and `run.settings_changed` in the run log (SETTINGS-9)
- [x] Confirm new remote hosts before saving (SETTINGS-10)
