# CLAUDE.md — Rifferr

Rifferr is a local web app for building and running **user-defined music-import workflows**, with beets as its primary library backend. The project is named Rifferr; its CLI command is `rifferr`. A workflow chains these step types:

- beets imports
- masstagger-style tag scripts
- shell commands
- multi-provider cover search
- optional AI tagging
- human review gates

It is a general, extensible tool. One person's workflow is an *example*, never the design.

- **Architecture:** `docs/architecture.md`. Read it before starting any non-trivial task.
- **Reference workflow:** `docs/original-checklist.md`, the author's beets + foobar2000 process. It must eventually be expressible as `examples/workflows/foobar-style.yaml`, which serves as the end-to-end acceptance test.

## Non-negotiables

1. **Nothing user-specific is hardcoded.** No folder paths, tag names, filenames, genre lists, provider choices or date conventions in engine code. They belong in profiles, workflow YAML, rules files or variables. If you're tempted to special-case the reference workflow, add a general capability instead and use it from the example workflow.
2. **The library is a pluggable backend.** The engine, steps, UI, covers and AI work only with snapshots and ChangeSets through the `LibraryBackend` protocol (architecture §8).
   - **Only `rifferr.backends.beets` may import beets.**
   - beets is a pinned PyPI dependency: **never fork, vendor or patch it**.
   - Use beets in-process only where the CLI can't do the job (interactive import, fast reads/preview, batch apply with undo, query/template evaluation). Everything else (`beets.command` steps) runs the `beet` CLI as a subprocess.
   - Before writing a feature, check whether beets or a bundled plugin already does it.
3. **All modifications are ChangeSets.** Tag scripts, shell steps, AI steps, chat and manual edits all produce ChangeSets. They apply through the engine, which writes the journal *before* changing anything. No other code path writes tags or moves files.
4. **AI proposes, never applies.** AI output is schema-validated and checked against the step's field allowlist and vocabulary. It then follows the step's `apply` policy, which defaults to `review`.
5. **Security.**
   - Host-header allowlist, Origin check and JSON-only mutating endpoints are **always on**, even with `--no-auth`.
   - The local token is invisible by default (URL → cookie). Non-localhost binding (Docker/remote) refuses to start without real auth.
   - Untrusted workflows get a static safety report and must be trusted by content hash before shell/python steps run. AI safety review is advisory and never grants trust.
   - Secrets live only in the keyring or environment. Never log them.
6. **Unattended first.** The main use is periodic, trigger-started imports on a self-hosted server. No step may require someone to be watching. Anything that needs a human goes to the review queue, pauses only that album's lane, and sends a notification.
7. **Built-ins use the public plugin APIs** (step types, cover providers, AI providers, template functions via entry points). If a built-in needs a private hook, the plugin API is missing something. Fix the API.

## License
AGPL-3.0-or-later. Before adding a dependency, confirm its license is AGPL-compatible (MIT/BSD/Apache-2.0/LGPL, or GPL *or-later*). Ask if unsure.

## Layout (summary — details in docs/architecture.md §9)

```
backend/src/rifferr/{core,engine,tagscript,steps,covers,ai,backends/{beets,files},worker,api}
frontend/            React + TS + Vite
docker/              Dockerfile, compose example, entrypoint (PUID/PGID)
schemas/             generated JSON Schemas — never hand-edit
examples/            workflows, rules, scripts
specs/  docs/
```
- `core`, `engine`, `tagscript`, `steps`, `covers` and `ai` never import a backend implementation, only `backends.base`. `core`, `engine` and `tagscript` are pure: no beets, no network, no filesystem except through injected interfaces. Run pyright in strict mode on them.
- beets runs in a **worker process per profile**, because beets' config is a process-wide singleton. The API server never touches beets directly.

## Commands

```bash
# repo (from root)
pre-commit install                             # once: gitleaks + spec lint on commit
pre-commit run --all-files
python3 scripts/spec.py list [--status Approved] [--milestone M1]
python3 scripts/spec.py show S-TAGSCRIPT -s Behavior   # one section, not the whole file
python3 scripts/spec.py behavior TAGSCRIPT-4           # one behavior line
python3 scripts/spec.py tests S-TAGSCRIPT              # behaviors with no test naming them
python3 scripts/spec.py lint

# backend (from backend/)
uv sync
uv run ruff format . && uv run ruff check .
uv run pyright
uv run pytest                                  # must pass before commit
uv run pytest -k "TAGSCRIPT_4"                 # one invariant
uv run rifferr validate ../examples/workflows/*.yaml
uv run python -m rifferr.schemas.export       # regenerate ../schemas/

# frontend (from frontend/)
pnpm install
pnpm gen:api            # regenerate TS types from backend OpenAPI
pnpm lint && pnpm typecheck && pnpm test
pnpm e2e                # Playwright; starts backend against a temp profile

# dev
uv run rifferr serve --profile dev --dev      # backend + Vite proxy
```

## Testing rules

- **No test touches real music or a real beets config.** Engine and step tests use an in-memory `FakeBackend`; beets-backend tests get a temp profile: `BEETSDIR`, library DB and music dir all under `tmp_path`.
- Audio fixtures are tiny generated FLAC/MP3 files in `backend/tests/fixtures/audio/`, regenerated by `scripts/make_fixtures.py`.
- **No network in tests.** Use `respx` for HTTP; record/replay fixtures for MusicBrainz and cover providers; a fake LLM provider for AI tests.
- Each tag-script action and each built-in example script needs a Hypothesis **idempotence** property test.
- `backends/beets` has **contract tests** pinning the beets behaviors we depend on (field semantics, ImportSession hooks, template functions). A beets version bump starts by running these.
- UI flows with a spec get a Playwright test: review queue, script preview, cover picker, import decisions.

## Spec-driven workflow

```
specs/
  _template.md
  000-overview.md            # derived from docs/architecture.md
  core/C-<NAME>.md           # data model, ChangeSet, journal, workflow schema
  steps/S-<NAME>.md          # one per step type
  features/F-<NAME>.md       # UI screens, API, CLI, security, packaging
docs/coverage.md             # reference-checklist line → how the example workflow does it
```

**Spec template:**
```markdown
# <ID>: Title
Status: Draft | Approved | Implemented | Verified
Milestone: M0–M5
## Context            (why; link architecture section)
## Behavior           (numbered, testable: "<ID>-1: The engine MUST …")
## Interfaces         (Pydantic models / API routes / YAML shape / UI states)
## Examples           (input → expected output; YAML snippets)
## Extensibility      (what a plugin author can override; what stays fixed)
## Security & safety
## Non-goals
## Open questions     (agent appends here instead of guessing)
## Tasks              (- [ ] items, ticked as work proceeds)
```

**Agent loop:**
1. Only humans move specs to **Approved**. You may draft specs and propose edits. Implement only Approved specs, in milestone order (architecture §11).
2. Read the spec (prefer `scripts/spec.py show <ID> -s <section>` over reading whole files), the architecture sections it links, and any neighboring specs it touches. If something is ambiguous, append it under *Open questions* and stop. Don't guess at user-facing behavior or music-domain conventions.
3. Tests first. Each behavior ID gets at least one test whose name includes the ID (`test_TAGSCRIPT_4_replace_regex_multivalue`). Behavior IDs drop the spec's category prefix: spec `S-TAGSCRIPT` has `TAGSCRIPT-1`, `TAGSCRIPT-2`, … (see `specs/000-overview.md`).
4. Implement the minimum code to pass, keeping to the layering rules above.
5. If public models changed, regenerate schemas and the TS client. Run every check for both backend and frontend.
6. Tick the *Tasks* items, set Status to Implemented, and update `docs/coverage.md` if the reference workflow gained a capability.
7. Make small commits, each prefixed with the spec ID (`S-SHELL: stream stderr to run log`).

## Conventions

- Workflow, script and rules files are YAML or Markdown edited by humans. Round-trip them with ruamel.yaml so comments and ordering survive UI edits.
- `{{ ... }}` is workflow-context interpolation. `$field` / `%func{}` is beets template syntax. Never mix the two in one evaluator.
- Every long operation emits structured events (`run.step.started`, `changeset.proposed`, …) and is cancellable. The UI never polls for progress; it subscribes over SSE.
- User-visible text names beets concepts plainly (query, template, flexible attribute) and links to beets docs, rather than inventing synonyms.
- Ask before adding a dependency, bumping beets, changing a published schema incompatibly, or loosening any security rule.
