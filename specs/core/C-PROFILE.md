# C-PROFILE: Profiles, variables and credential references
Status: Implemented
Milestone: M0

## Context
A profile bundles a beets config, a library DB, named variables (folders etc.),
provider settings and references to credentials (architecture §2). Profiles
are how "nothing user-specific is hardcoded" is achieved: every path or
convention a workflow needs comes from a profile variable or the workflow
itself. Native installs use XDG dirs; Docker uses `/config` (architecture §8,
install modes).

## Behavior
- PROFILE-1: Each profile MUST be a directory `<config_root>/profiles/<name>/` holding `profile.yaml`; `<config_root>` is `RIFFERR_CONFIG_DIR` when set (the Docker image sets it to `/config`), else `$XDG_CONFIG_HOME/rifferr`, else `~/.config/rifferr`.
- PROFILE-2: `profile.yaml` MUST declare `backend` (default `beets`), backend settings (for beets: `beetsdir`, which MAY point at an existing beets config directory), `vars` (string map) and `secrets` (map of name to reference).
- PROFILE-3: Profile names MUST match `^[a-z0-9][a-z0-9_-]{0,63}$`; anything else is rejected.
- PROFILE-4: A secret reference MUST be one of `keyring:<service>/<name>`, `env:<VAR>` or `file:<absolute path>`. Literal secret values in `profile.yaml` MUST be a validation error, and the error message MUST NOT repeat the value.
- PROFILE-5: Resolving a secret MUST happen only at the point of use, and resolved values MUST never be included in snapshots, events, logs, run records, API responses or validation messages.
- PROFILE-6: Profile `vars` MUST be exposed to workflows as `{{ vars.<name> }}` (C-WORKFLOW) and to shell steps as `RIFFERR_VAR_<NAME>` (S-SHELL), with workflow-declared defaults used only when the profile doesn't set the variable.
- PROFILE-7: `vars` values MAY reference other vars and environment variables with `{{ vars.x }}` / `{{ env.HOME }}`; cycles MUST be a validation error.
- PROFILE-8: `rifferr profile create <name>` (F-CLI) MUST create a profile with empty `vars` and `secrets`, a `workflows/` directory, and `beets.beetsdir` set only when `--beetsdir` is given. It MUST NOT create, modify or default any library DB or music directory, and MUST NOT modify an existing beets config. A profile without `beets.beetsdir` loads fine, but anything that needs the library MUST fail with an error telling the user to set it; Rifferr never falls back to beets' own defaults (`~/Music`, `~/.config/beets`).
- PROFILE-9: Loading a profile MUST NOT import beets or touch the beets library; that happens only in the worker (F-WORKER).
- PROFILE-10: A profile's effective time zone MUST be its `timezone` (an IANA name, validated on load) when set, otherwise the system's local time zone.
- PROFILE-11: A `file:` secret MUST be read only at the point of use, with one trailing newline stripped. Rifferr MUST refuse to read it if it isn't a regular file, or if it is readable by other users and not under `/run/secrets/` (where Docker manages permissions).

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
timezone: Europe/Amsterdam           # optional; default: system time zone
# docker: discogs_token: file:/run/secrets/discogs_token
```
```python
class SecretRef(BaseModel):     # parsed from "keyring:svc/name" | "env:VAR" | "file:/path"
    kind: Literal["keyring", "env", "file"]
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

As implemented:
- `rifferr.core.profile` (pure): `ProfileFile`, `Profile`, `SecretRef`, `Secret` (repr/str show
  `***`), `Redactor` (masks values of 4+ characters), `resolve_vars`, `merged_vars`,
  `env_var_name`, `parse_profile`.
- `rifferr.credentials`: `SecretResolver` for `env:`, `keyring:` (keyring imported lazily) and
  `file:`. Errors name the reference, never the value.
- `rifferr.profile_store`: `config_root`, `load_profile`, `list_profiles`, `create_profile`,
  `effective_timezone`. `~` in `beets.beetsdir` expands to `$HOME`.
- `Profile.require_beetsdir()` raises the "set beets.beetsdir" error (PROFILE-8).
- Var names match `^[A-Za-z_][A-Za-z0-9_]*$` and values must be strings (quote numbers).

## Examples
- `secrets: { x: "abc123" }` → error `secrets.x: must be keyring:… or env:…, not a literal value`.
- Workflow declares `vars.inbox` with no default, profile sets `inbox` → resolved; profile doesn't → C-WORKFLOW validation error (WORKFLOW-7).

## Extensibility
Secret reference kinds are fixed in M0 (`keyring`, `env`, `file`). Backend settings blocks are keyed by backend name so other backends can add their own.

## Security & safety
- PROFILE-4/5 keep secrets out of files and output.
- Profile files are user-owned config; they are trusted like the user's beets config, not subject to workflow trust.

## Non-goals
- Provider settings for covers and AI (S-COVERS-SEARCH, S-AI-TAG).
- Profile editing UI (later).

## Open questions
- ~~Library DB / music dir defaults for a new profile?~~ Resolved 2026-11-11 (author): none; see PROFILE-8.
- ~~Timezone per profile?~~ Resolved 2026-11-11 (author): system time zone with optional per-profile override (PROFILE-10).
- ~~`file:` secret references in M0?~~ Resolved 2026-11-11 (author): yes (PROFILE-4, PROFILE-11).
- Dependency keyring (MIT) approved 2026-11-11 (author).

## Tasks
- [x] profile models + loader
- [x] secret reference parsing + resolver (keyring, env, file)
- [x] profile store: config root, load, list, create
- [x] effective time zone
- [x] var resolution with cycle detection
- [x] redaction helper used by logs/events
