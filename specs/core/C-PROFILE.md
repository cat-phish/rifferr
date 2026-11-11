# C-PROFILE: Profiles, variables and credential references
Status: Draft
Milestone: M0

## Context
A profile bundles a beets config, a library DB, named variables (folders etc.),
provider settings and references to credentials (architecture §2). Profiles
are how "nothing user-specific is hardcoded" is achieved: every path or
convention a workflow needs comes from a profile variable or the workflow
itself. Native installs use XDG dirs; Docker uses `/config` (architecture §8,
install modes).

## Behavior
- PROFILE-1: Each profile MUST be a directory `<config_root>/profiles/<name>/` holding `profile.yaml`; `<config_root>` defaults to `$XDG_CONFIG_HOME/rifferr` natively and `/config` in Docker, and can be overridden by `RIFFERR_CONFIG_DIR`.
- PROFILE-2: `profile.yaml` MUST declare `backend` (default `beets`), backend settings (for beets: `beetsdir`, which MAY point at an existing beets config directory), `vars` (string map) and `secrets` (map of name to reference).
- PROFILE-3: Profile names MUST match `^[a-z0-9][a-z0-9_-]{0,63}$`; anything else is rejected.
- PROFILE-4: A secret reference MUST be one of `keyring:<service>/<name>` or `env:<VAR>`. Literal secret values in `profile.yaml` MUST be a validation error.
- PROFILE-5: Resolving a secret MUST happen only at the point of use, and resolved values MUST never be included in snapshots, events, logs, run records, API responses or validation messages.
- PROFILE-6: Profile `vars` MUST be exposed to workflows as `{{ vars.<name> }}` (C-WORKFLOW) and to shell steps as `RIFFERR_VAR_<NAME>` (S-SHELL), with workflow-declared defaults used only when the profile doesn't set the variable.
- PROFILE-7: `vars` values MAY reference other vars and environment variables with `{{ vars.x }}` / `{{ env.HOME }}`; cycles MUST be a validation error.
- PROFILE-8: `rifferr profile create <name>` (F-CLI) MUST create a profile with an empty `vars` map and a new beets directory under the profile, or link to an existing beets directory when `--beetsdir` is given, and MUST NOT modify the existing beets config.
- PROFILE-9: Loading a profile MUST NOT import beets or touch the beets library; that happens only in the worker (F-WORKER).

## Interfaces
```yaml
# <config_root>/profiles/main/profile.yaml
backend: beets
beets:
  beetsdir: ~/.config/beets          # existing beets setup, used as-is
vars:
  inbox: /srv/downloads/music
  library: /srv/music
secrets:
  discogs_token: keyring:rifferr/discogs
  anthropic_api_key: env:ANTHROPIC_API_KEY
timezone: Europe/Amsterdam           # used by schedule triggers (M2)
```
```python
class SecretRef(BaseModel):     # parsed from "keyring:svc/name" | "env:VAR"
    kind: Literal["keyring", "env"]
    target: str

class Profile(BaseModel):
    name: str
    root: Path
    backend: str = "beets"
    beets: BeetsSettings | None
    vars: dict[str, str] = {}
    secrets: dict[str, SecretRef] = {}
    timezone: str | None = None

class SecretResolver(Protocol):
    def resolve(self, ref: SecretRef) -> str: ...
```

## Examples
- `secrets: { x: "abc123" }` → error `secrets.x: must be keyring:… or env:…, not a literal value`.
- Workflow declares `vars.inbox` with no default, profile sets `inbox` → resolved; profile doesn't → C-WORKFLOW validation error (WORKFLOW-7).

## Extensibility
Secret reference kinds are fixed in M0 (`keyring`, `env`); Docker secrets are read as `env:` or a later `file:` kind. Backend settings blocks are keyed by backend name so other backends can add their own.

## Security & safety
- PROFILE-4/5 keep secrets out of files and output.
- Profile files are user-owned config; they are trusted like the user's beets config, not subject to workflow trust.

## Non-goals
- Provider settings for covers and AI (S-COVERS-SEARCH, S-AI-TAG).
- Profile editing UI (later).

## Open questions
- Does a new profile with no `--beetsdir` get its own library DB and music directory defaults, and what should those defaults be (they are user-specific, so maybe must be asked at creation)?
- Is `timezone` per profile right, or should it default from the system and only be overridable?
- Should `file:` secret references (Docker secrets at `/run/secrets/…`) be in M0?

## Tasks
- [ ] profile models + loader
- [ ] secret reference parsing + resolver (keyring, env)
- [ ] var resolution with cycle detection
- [ ] redaction helper used by logs/events
