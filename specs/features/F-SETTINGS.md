# F-SETTINGS: Settings page, AI section first
Status: Draft
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
  runs that start afterwards. A run already going keeps the settings it started with.
- SETTINGS-5: The AI section MUST show and edit: the providers (name, type from the installed
  `rifferr.ai_providers`, model, `base_url`, enabled, order; the first enabled one is the
  default), `concurrency`, `timeout`, `cache_ttl`, the lookalike file (`ai.lookalikes`,
  AI-CORE-22), and the suggestion mode `off | unsure | always` with per-step overrides
  (AI-SUGGEST-7). The override list offers the profile's workflow steps that have `with.ai`.
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
  host isn't local, as the safety report does (AI-CORE-20).

## Non-goals
- Editing workflows (B-078) or rules files.
- Settings other than AI in M4.

## Open questions
1. Should changing a provider's host or adding a remote provider ask for confirmation, like
   trusting a workflow? This draft only shows a notice.
2. Keyring name: `rifferr/<profile>/<provider>`, or let the user type a name?

## Tasks
- [ ] Section registry, GET/PUT with schema, ruamel write, 409 (SETTINGS-1 to -4)
- [ ] AI section form (SETTINGS-5)
- [ ] Write-only keys with keyring and env fallback (SETTINGS-6)
- [ ] Test connection (SETTINGS-7)
- [ ] Security checks and Playwright test (SETTINGS-8)
