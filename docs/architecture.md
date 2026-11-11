# Rifferr — Architecture (seed for specs/000-overview)

> Status: Draft. Every section here becomes, or is refined by, a spec in `specs/`.
## 1. What it is

Rifferr is a **local web app for building and running music-import workflows**, with beets as its primary library backend. Project name: Rifferr. CLI command, Python package, Docker image: `rifferr`. A workflow is an ordered chain of steps that the user defines:

- beets imports
- tag scripts
- shell commands
- cover-art searches
- AI-assisted tagging
- human review checkpoints

Workflows are plain YAML files. They can be shared, versioned in git, and edited either in the UI or by hand.

The author's own beets + foobar2000 checklist (`docs/original-checklist.md`) is the **reference workflow** and acceptance test. It must be fully expressible as `examples/workflows/foobar-style.yaml`. It must never be hardcoded into the engine.

### Design tenets
1. **Nothing is hardcoded.** Folders, filenames, tag names, providers and genre vocabularies all come from profiles, workflow files or variables.
2. **Library backends are pluggable; beets is the first and primary one.** The engine, UI, tag scripts, shell steps, cover search and AI work only with snapshots and ChangeSets, never with beets objects. beets sits behind the `LibraryBackend` interface (§8). Where beets is the backend, users keep their beets config, plugins, query syntax and template syntax, and the UI names those concepts plainly.
   - **beets is an ordinary pinned dependency, not a fork.** We never copy, vendor or patch beets code. The only maintenance is keeping `backends/beets` compatible with new beets releases, guarded by contract tests.
3. **Every tag change is a reviewable `ChangeSet`.** Scripts, shell commands, AI steps and manual edits all produce ChangeSets. Each step's policy decides whether its ChangeSets auto-apply or wait for review. Every apply is journaled and can be undone.
4. **Extensible by default.** Step types, cover providers, AI providers and template functions are all plugins, registered through Python entry points. Built-in features use the same plugin API as third-party ones.
5. **Local-first and safe.** The app runs on the user's machine (or their own server). It never phones home. AI and network providers are opt-in.
6. **Self-hosted and unattended first.** The main ongoing use is *periodic import of newly added music* on a home server, usually in Docker. Bulk import of an existing collection is supported but secondary. Runs start from triggers (§2a) without anyone watching. Albums that need a decision wait in the review queue without blocking the rest, and the user is notified.

### Prior art
beets-flask (MIT, github.com/pSpitzner/beets-flask) is a web UI around beets import. It subclasses beets' `ImportSession` to drive imports from a browser, and supports import previews, auto-import of good matches, undo and multiple inboxes. Study it before designing `F-IMPORT-UI`. Rifferr's differentiators are workflow chaining, tag scripting, multi-provider cover search and AI steps.

## 2. Core concepts

| Concept | Definition |
|---|---|
| **Profile** | A beets config + library DB + named variables (folders etc.) + provider settings + credentials references. A user can have several (e.g. "main library", "test"). |
| **Workflow** | YAML file: metadata, declared variables, ordered `steps`. Can call other workflows (`uses: workflow`). |
| **Step** | `{id, uses: <step-type>, with: {...params}, when: <beets query>, apply: auto|review, on_error: stop|skip|continue}` |
| **Step type** | Plugin implementing the Step protocol (validate params, run, emit events/ChangeSets). |
| **Run** | One execution of a workflow against a profile. Has a state machine, an event log, a review queue, a journal. Can be resumed and undone. |
| **Trigger** | What starts a run: folder watch, schedule, webhook or manual (§2a). Declared in the workflow's `on:` block. |
| **Lane** | One album's path through a run. Lanes progress independently, so an album waiting at a review gate doesn't hold up the others. |
| **Selection** | Which albums/items a step acts on. Default: albums touched by this run (tracked via flex attr `rifferr_run`). A step's `when` narrows this further with a beets query. |
| **ChangeSet** | Proposed field changes (album- or item-scoped, old → new) plus optional file ops, a rationale and a source step. The single currency of modification. |
| **Review queue** | ChangeSets and decisions (import matches, cover picks, AI suggestions) waiting for the user. |
| **Journal** | Append-only record of applied ChangeSets with old values. It is what undo replays. |

### Interpolation (two syntaxes, deliberately distinct)
- `{{ vars.inbox }}`, `{{ run.id }}`: **workflow context**, resolved when a step starts.
- `$album`, `%if{$comp,...}`: **beets template syntax**, evaluated per album/item in tag scripts, shell args and filenames. Rifferr template functions are registered as beets template functions, so they also work in beets path formats.

## 2a. Triggers and unattended runs

```yaml
name: new-music
on:
  watch:    { path: "{{ vars.inbox }}", settle: 10m }   # start once a folder stops changing
  schedule: "0 4 * * *"                                 # also sweep nightly
  webhook:  { name: lidarr }                            # POST /hooks/lidarr (token-authenticated)
steps: [...]
```

- **watch:** Uses inotify through `watchfiles`. Falls back to polling for network mounts, where inotify doesn't work (common with NAS shares in Docker). The *settle* period waits until a folder has stopped changing, so half-copied downloads aren't imported.
- **schedule:** cron syntax, evaluated in the profile's time zone.
- **webhook:** an authenticated endpoint for download managers and other tools. Lidarr's Connect webhooks and slskd are the first documented examples, and the payload's path becomes the run's input. Each webhook has its own secret, separate from the UI login.
- **manual:** from the UI or `rifferr run`.
- **Deduplication:** each input folder is fingerprinted (paths, sizes, mtimes). A folder that is already in progress or already imported isn't started again unless it changes or the user forces it.
- **Concurrency:** one active run per profile by default, and further triggers queue up. Within a run, lanes run in parallel up to a limit.
- **Unattended policy:**
  - `beets.import` defaults to `mode: auto`. Strong matches are applied; ambiguous albums are routed to the review queue as import decisions, and their lanes pause.
  - Steps with `apply: review` pause only the affected lanes.
  - A run is *complete* when every lane is finished or waiting for review. Waiting lanes resume when the user resolves them, even days later.
- **Notifications:** a built-in `notify` step and global notification rules. Apprise covers ntfy, Gotify, Discord, email, Matrix, webhooks and more. Default events: review needed, run failed, run summary. Notification links open the exact item in the review queue.
- **Dashboard:** the home screen is the queue: the inbox status, runs in progress, items needing review, and recent imports with one-click undo.

## 3. Built-in step types

| `uses:` | Purpose | Notes |
|---|---|---|
| `beets.import` | Import a folder | `mode: quiet | auto | interactive`. Interactive decisions (candidate choice, as-is, skip, search by ID) are made in the UI via a custom `ImportSession`. |
| `beets.command` | Run any beet subcommand on the selection | e.g. `replaygain -a`, `fetchart`, `move`, `mbsync`. Runs the `beet` CLI as a **subprocess** with the profile's `BEETSDIR`, exactly as in a terminal. The selection is passed as a query (`rifferr_run:<id>`). Output streams to the run log. |
| `tags.script` | Masstagger-style tag actions | See §4. |
| `shell` | Run any command/script | See §5. |
| `covers.search` | Multi-provider cover search | Auto-pick by score, or queue for UI choice. See §6. |
| `ai.tag` | LLM-assisted field fill | See §7. |
| `review` | Explicit gate | Pauses each lane until its queued items are resolved. |
| `notify` | Send a notification | Through Apprise URLs from the profile; message is templated. |
| `files.cleanup` | Report/delete leftover files and empty dirs in a folder | Dry-run listing first. |
| `workflow` | Call another workflow | Variables passed explicitly. |

Any step with `apply: review` pauses the affected lanes after that step until their ChangeSets are resolved, because later steps may read those fields. Other lanes continue.

## 4. Tag scripting (`tags.script`)

The goal is foobar2000 Masstagger's power with less ceremony. A script is an **ordered list of actions**. Each action has an optional `when` (beets query) and a scope (`album` or `item`).

```yaml
uses: tags.script
with:
  scope: album
  actions:
    - when: 'albumtype:ep ^album::\bEP\b'
      set: { album: '$album (EP)' }
    - copy: { from: genre, to: subgenre }
    - replace: { fields: [album, title], find: '/', with: '∕' }
    - replace: { fields: [artist], regex: '\s+feat\..*$', with: '' }
    - split: { field: albumartist, by: ' / ', into: albumartists }
    - from_filename: { pattern: '$track - $title' }
    - case: { field: title, style: title }
    - delete: [comment]
```

Action set (v1): `set`, `copy`, `move`, `replace` (literal or regex), `split`, `join`, `case`, `from_filename`, `delete`, `append_unique` (multi-value).

### UI requirements
- A visual action-list builder with field autocomplete, plus a raw-YAML toggle. Both edit the same file; use ruamel.yaml so comments are preserved.
- A condition builder that writes beets queries, so users don't have to learn query syntax up front.
- A **live preview grid**: before/after for the current selection, updated on every edit.
- An **idempotence check** in preview: run the script twice, and if the second pass changes anything, highlight the action responsible.
- Scripts can be saved as named, reusable files and referenced from workflows (`script: scripts/release-types.yaml`).

Escape hatch: a `python:` action pointing at a function in a user plugin module. This is not v1.

## 5. Shell steps

```yaml
uses: shell
with:
  run: ['my-script.sh', '$path']   # argv list (preferred) or string with shell: true
  per: album                       # run | album | item
  timeout: 300
  emits: changes                   # none | changes
```
- Environment: `RIFFERR_RUN_ID`, `RIFFERR_PROFILE`, `RIFFERR_ALBUM_DIR`, `RIFFERR_ITEM_PATH`, workflow vars as `RIFFERR_VAR_<NAME>`.
- stdin: JSON snapshot of the album/item (schema published from Pydantic models).
- `emits: changes`: stdout is parsed as a JSON ChangeSet (published schema), which then goes through the normal apply/review path. This lets any script in any language act as a first-class tagger.
- stdout/stderr stream into the run log. Exit-code handling follows `on_error`.

## 6. Cover search

- Provider protocol: `async search(query: CoverQuery, settings) -> AsyncIterator[CoverCandidate]`. A candidate carries the image URL, thumbnail, dimensions, provider, match confidence and source page.
- Initial providers: Cover Art Archive, iTunes, Deezer, Discogs (token), fanart.tv (key), Last.fm (key), local files in the album folder. Use only official APIs and respect provider terms.
- Ranking: `score = provider_weight × match_confidence × size_score`, with hard filters (minimum size, aspect-ratio tolerance). Weights, filters and the enabled-provider order are saved per profile.
- UI:
  - a grid that streams results as providers respond
  - editable query (artist, album, year, catalog number)
  - per-provider toggles and weight sliders
  - full-size preview with dimensions and file size
  - one-click choose
- Output settings: filename template (e.g. `cover.jpg`), format/quality, max size, optional embed (via beets `embedart`).
- Results and thumbnails are cached per query.

## 7. AI integration (optional)

- **Providers:** two adapters, Anthropic and OpenAI-compatible. OpenAI-compatible also covers OpenAI, Ollama, LM Studio and OpenRouter. Configured per profile. Keys come from the OS keyring or environment, never from workflow files.
- **Rules files:** user-authored Markdown, like an AGENTS.md (e.g. `rules/genres.md`), plus optional vocabularies (`rules/genre-allowlist.txt`, a denylist).
- **`ai.tag` step:**
  ```yaml
  uses: ai.tag
  apply: review
  with:
    fields: [genre, subgenre]          # allowlist: the model may only propose these
    context: [albumartist, album, year, label, genre, mb_albumid]
    rules: rules/genres.md
    vocabulary: { genre: rules/genre-allowlist.txt }
  ```
  - Structured output only (tool call / JSON schema) → ChangeSet with per-field rationale and confidence.
  - **Validated before it reaches the user.** Out-of-allowlist fields or out-of-vocabulary values are rejected or flagged.
  - Cached by (album fingerprint, rules hash, model, prompt version).
- **Chat panel:** a conversation scoped to the current selection.
  - Tools: read tags, run beets queries, preview a tag script, propose a ChangeSet, append a rule to a rules file (requires confirmation).
  - **The chat can propose, never apply.** Everything goes to the review queue.
- **Transparency:** the UI can show exactly what will be sent before it's sent. A per-run token/cost estimate is shown when the provider reports usage.

## 8. System architecture

```
 Browser (React SPA) ──HTTP/SSE──▶ API server (FastAPI, 127.0.0.1)
                                    │  app DB (SQLite, WAL): runs, events,
                                    │  review queue, journal, caches, settings
                                    ▼
                               Worker process (one per active profile)
                                    │  engine executes steps
                                    │  ── LibraryBackend interface ──
                                    │     BeetsBackend │ FilesBackend (later)
                                    ▼
                         beets library DB + music files │ plain folders
```

### LibraryBackend interface
The engine's only view of a music library:

```python
class LibraryBackend(Protocol):
    capabilities: BackendCapabilities           # import? queries? templates? undo?
    def query(self, selection: Selection) -> list[AlbumSnapshot]: ...
    def evaluate(self, template: str, target: ItemSnapshot | AlbumSnapshot) -> str: ...
    def matches(self, condition: str, target: ItemSnapshot | AlbumSnapshot) -> bool: ...
    def apply(self, changes: ChangeSet) -> JournalEntry: ...   # returns old values for undo
    def import_folder(self, path: Path, mode: ImportMode,
                      decide: DecisionCallback) -> ImportResult: ...
    def command_env(self) -> Mapping[str, str]: ...            # env for CLI subprocess steps
```
- Steps declare which capabilities they need. A workflow is validated against the profile's backend before it runs (e.g. `beets.import` requires `import`).
- Template and condition syntax belong to the backend: beets syntax under `BeetsBackend`. The FilesBackend offers a compatible subset and states its gaps in its spec.

### BeetsBackend (`backends/beets`), the only code that imports beets
beets is used **in-process only where running the CLI can't do the job**. Everything else runs the `beet` CLI.

| beets API used | Why in-process |
|---|---|
| `beets.importer.ImportSession` (subclass: `choose_match`, `choose_item`, `resolve_duplicate`, `should_resume`) | The CLI asks import questions as terminal prompts. The subclass receives candidates as structured data, so the UI can render them and send the choice back. |
| `beets.library.Library`, `lib.albums()/items()` | Fast structured reads for the tag grid and live previews. CLI startup per call is too slow. |
| `Item/Album` field access, `store()`, `try_write()` (mediafile underneath) | Batch-apply ChangeSets while capturing old values. Correct per-format tag mapping and multi-valued fields. |
| Query parsing, `evaluate_template()` | Tag-script conditions and values use exact beets syntax, including plugin-provided template functions, fast enough for preview-as-you-type. |
| Config + plugin loading | Required by everything above. It uses semi-internal beets functions, so it's isolated in one module and covered by contract tests. |

- **Why a separate worker:** beets' configuration and plugin registry are process-wide singletons. One worker process per profile keeps profiles isolated and keeps blocking beets calls off the API event loop.
- **CLI steps vs. in-process access:** the worker releases or reopens its Library handle around `beets.command` subprocesses, so the two never hold competing SQLite write locks.
- **Contract tests** pin every beets behavior we rely on (field semantics, ImportSession hooks, template functions, query parsing). They're the first thing run when bumping beets.

### FilesBackend (`backends/files`), later milestone
Reads and writes tags directly with mutagen on plain folders, with no beets library. Uses: tagging leftovers that beets couldn't match, pre-import cleanup, and users without beets. Supports `query`/`evaluate`/`matches`/`apply`. Doesn't support `import_folder`.

- **Server ↔ worker:** job/command table in the app DB plus a local IPC channel for events and interactive-import decisions. The server fans events out to the UI over SSE.

### Security (non-negotiable; the app can run shell commands)
The threat is not the app's power; it's *who can trigger it*. A shell script only runs when the user runs it. A local web server can be reached by any web page open in the user's browser (cross-site requests, DNS rebinding) and by other users and processes on the same machine.
- **Always on, zero friction:**
  - bind to 127.0.0.1 by default
  - allowlist the Host header (defeats DNS rebinding)
  - reject requests whose Origin isn't the app's own
  - mutating endpoints accept only `application/json`, so plain HTML form posts can't reach them
  - no CORS
- **Local auth token (default on, invisible):** generated at launch. `rifferr serve` opens the browser with the token in the URL, the app exchanges it for an HttpOnly cookie, and the user never types it (the same approach Jupyter uses). `--no-auth` disables it for single-user machines; the Host/Origin checks stay on.
- **Remote / Docker mode:** binding to anything other than localhost *requires* real authentication (username + password, or delegating to a reverse proxy via a trusted header). The server refuses to start otherwise.
- **Workflow trust:** workflows, scripts and rules files that didn't originate in this install are untrusted. Before their first run, the UI shows a **safety report** and the user must trust that exact content hash. Any edit to the content resets the trust.
  - *Static analysis (always):* every `shell` command and `python` action, with templates expanded for a sample album; paths touched outside the library and inbox; network tools (`curl`, `wget`, `ssh`, …); destructive commands (`rm`, `mv` outside the album, `chmod`, `sudo`); steps with `apply: auto` that write tags.
  - *AI review (optional):* a plain-language explanation of what the workflow does, plus flagged concerns. It is **advisory only**: workflow files can contain text written to mislead the reviewer (prompt injection, e.g. a comment saying "this step is safe"). The AI review never sets trust and never hides static-analysis findings.
- Secrets go in the keyring (native) or Docker secrets/env (container) and are never written to logs, events or workflow files.

### Install modes
- **Standalone (native):** `uv tool install rifferr` (or pipx). This installs Rifferr, beets and Python dependencies into an isolated environment. The user installs system tools (ffmpeg, optional chromaprint) with their package manager; the app checks for them at startup and reports what's missing. The profile can point at the user's **existing beets config and library**. Extra beets plugins from PyPI are added with `uv tool install rifferr --with beets-<plugin>`, or from the UI's plugin page.
- **Docker:** same app, with every system tool and common plugin dependency preinstalled (see below).
- Both modes use the same code, config format and workflow files. Only the default paths differ (XDG dirs natively, `/config` in Docker).

### Docker mode
- The image runs as a configurable UID/GID (`PUID`/`PGID`) so files keep the user's ownership. Volumes: `/config` (profiles, beets config, app DB), `/music` (library), plus any inbox folders. Folder paths in profiles refer to *container* paths.
- `shell` steps run **inside the container**. They can only see mounted folders and installed tools. Mount a scripts folder, and document extending the image (`FROM rifferr` + `apt install …`) for extra tools. Desktop-only commands like `notify-send` won't work; provide a built-in notification step (webhook / ntfy / Apprise) instead.
- Health check endpoint; graceful shutdown that pauses runs and resumes them on restart.

## 9. Repository layout

```
backend/
  pyproject.toml
  src/rifferr/
    core/           # pure: snapshots, ChangeSet, workflow schema (Pydantic), selection
    engine/         # run state machine, step registry, review queue, journal/undo
    tagscript/      # action models, evaluator, preview, idempotence check
    steps/          # built-in step types (each uses the public Step API)
    covers/         # provider protocol, providers, ranking, cache
    ai/             # provider adapters, ai.tag, chat agent + tools
    backends/
      base.py       # LibraryBackend protocol, capabilities
      beets/        # BeetsBackend — the ONLY package importing beets
      files/        # FilesBackend (mutagen) — later milestone
    worker/         # worker process entrypoint + IPC
    api/            # FastAPI routers, SSE, auth
    cli.py          # `rifferr serve`, `rifferr run <workflow>`, `rifferr validate`
  tests/
frontend/
  src/ (React + TS), tests (Vitest), e2e (Playwright)
schemas/            # generated JSON Schemas (workflow, ChangeSet, snapshot)
examples/workflows/ # incl. foobar-style.yaml (reference workflow)
examples/rules/     # sample genre rules + vocabularies
specs/  docs/
```

## 10. Tooling

| Area | Choice |
|---|---|
| Backend | Python ≥ 3.12, uv, FastAPI, Pydantic v2, httpx, ruamel.yaml, keyring, SQLite (stdlib + simple migrations) |
| beets | Regular dependency from PyPI, pinned to a compatible range (e.g. `beets>=2.14,<2.15`). Never forked or vendored. Bumps require a spec and passing contract tests. |
| Frontend | TypeScript, React, Vite, TanStack Query + Table + Virtual (tag grid for thousands of rows), component library TBD |
| API contract | FastAPI OpenAPI → `openapi-typescript` generated client types; Pydantic → JSON Schema for workflow files (editor autocomplete + UI forms) |
| Quality | ruff, pyright (strict on core/engine/tagscript), eslint, prettier, tsc --noEmit |
| Tests | pytest, hypothesis, respx (HTTP mocks), Vitest + Testing Library, Playwright e2e |
| System deps | ffmpeg (ReplayGain, fixtures) |
| Distribution | Both are first-class: (1) native install via PyPI with the built frontend bundled (`uv tool install rifferr`; user installs ffmpeg etc.); (2) Docker image with beets, ffmpeg, chromaprint and common plugin dependencies preinstalled. Desktop wrapper (pywebview/Tauri) optional, later. |
| License | AGPL-3.0-or-later. All dependencies must be AGPL-compatible: MIT/BSD/Apache-2.0/LGPL, or GPL **"or later"** (e.g. mutagen, GPL-2.0-or-later). Check new deps before adding. |

## 11. Milestones

| M | Scope | Done when |
|---|---|---|
| M0 | Skeleton: profile config, worker + adapter, API, UI shell, `beets.import` (quiet) + `shell` steps, run log streaming | A two-step workflow runs from the UI and the CLI |
| M1 | ChangeSet, journal, undo, review queue, library/tag grid, `tags.script` with live preview | The release-type suffix logic runs as a script, with an idempotence check |
| M2 | Interactive import decisions in the UI; triggers (watch, schedule, webhook), lanes, dedup, notifications; queue dashboard | New music dropped in the inbox imports unattended. An ambiguous album waits in review, triggers a notification, and doesn't block the others. |
| M3 | Cover search UI + `covers.search` | Pick art from at least 3 providers with weights |
| M4 | `ai.tag` + chat | Genre step proposes vocabulary-valid values into review |
| M5 | Reference workflow end-to-end, PyPI + Docker packaging | `examples/workflows/foobar-style.yaml` reproduces the original checklist |

## 12. Open questions
- Deeper *arr-ecosystem integration beyond the webhook trigger, as an `integrations` plugin type. For example: tell Lidarr to rescan after an import, read Lidarr's wanted list to flag incomplete albums, or update Navidrome/Jellyfin after a run.
- Component library (shadcn/ui, Mantine, …) and whether to add a desktop wrapper in v1.
- Exact IPC mechanism between server and worker (multiprocessing pipes vs local socket).
- Whether `when:` should also accept a simpler expression language that compiles to beets queries.
- Whether to add a plugin exception to the AGPL so in-process plugins can use any license (shell-step scripts are separate programs and unaffected either way).
