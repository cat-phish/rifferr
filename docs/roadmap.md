# Rifferr roadmap

Living plan for building Rifferr. Update it when a milestone starts, when the
spec order changes, and when a milestone is done. Architecture lives in
[architecture.md](architecture.md); working rules live in
[AGENTS.md](../AGENTS.md); the spec index lives in
[specs/000-overview.md](../specs/000-overview.md); deferred ideas and issues
live in [BACKLOG.md](../BACKLOG.md).

## How we build

1. **One milestone at a time** (architecture §11). Specs for the current
   milestone are drafted first, as `Status: Draft`.
2. **A human approves each spec.** Agents implement only Approved specs, in the
   order listed below. Ambiguity goes under the spec's *Open questions*; the
   agent stops instead of guessing.
3. **Tests first.** Every behavior ID gets a test whose name carries the ID
   (`test_SHELL_3_timeout_kills_process_group`). `scripts/spec.py tests <ID>`
   lists behaviors with no test yet.
4. **Vertical slices.** Each milestone ends with something runnable end to
   end, not a finished layer.
5. **Done means:** all checks pass for backend and frontend, the spec's Tasks
   are ticked, Status is Implemented, and `docs/coverage.md` is updated if the
   reference workflow gained a capability.
6. Things found along the way that are outside the current spec go in
   `BACKLOG.md`, not into the current change.

## Prerequisites

| Item | Needed by | State |
|---|---|---|
| Real `uv` on PATH (installed; the shell still aliases `uv` to an editor) | M0 | Present |
| `pnpm` | M0 | Present |
| ffmpeg (fixtures, ReplayGain) | M0 | Present |
| `docs/original-checklist.md` filled in by the author | M1 (release-type suffix logic), M5 | Placeholder |
| Decisions in M0 specs' *Open questions* | M0 approval | Open |

## Dependency ledger

Every runtime or dev dependency needs the author's OK before it is added
(AGENTS.md). Licenses must be AGPL-compatible.

| Package | Use | License | Milestone | Approved |
|---|---|---|---|---|
| beets (`>=2.14,<2.15`) | library backend | MIT | M0 | |
| fastapi | API server | MIT | M0 | |
| uvicorn | ASGI server | BSD-3-Clause | M0 | |
| pydantic v2 | models, JSON Schema | MIT | M0 | 2026-11-11 |
| ruamel.yaml | round-trip YAML | MIT | M0 | 2026-11-11 |
| keyring | secrets | MIT | M0 | 2026-11-11 |
| httpx | HTTP client | BSD-3-Clause | M0 | |
| pytest, pytest-asyncio | tests | MIT / Apache-2.0 | M0 | 2026-11-11 |
| hypothesis | property tests (dev only) | MPL-2.0 | M0 | 2026-11-11 |
| respx | HTTP mocks (dev only) | BSD-3-Clause | M0 | 2026-11-11 |
| ruff, pyright | lint, types (dev only) | MIT | M0 | 2026-11-11 |
| react, react-dom, vite, typescript | frontend | MIT / Apache-2.0 | M0 | 2026-11-11 |
| @tanstack/react-query | data fetching | MIT | M0 | |
| openapi-typescript | API client types | MIT | M0 | 2026-11-11 |
| vitest, @testing-library/react | frontend tests | MIT | M0 | 2026-11-11 |
| @playwright/test | e2e | Apache-2.0 | M0 | |
| eslint, prettier | frontend lint | MIT | M0 | 2026-11-11 |
| @vitejs/plugin-react, @types/react, @types/react-dom, @types/node, @eslint/js, typescript-eslint, eslint-plugin-react-hooks, globals, @testing-library/jest-dom, jsdom | companions of the approved frontend tools | MIT | M0 | 2026-11-11 (with F-DEV-TOOLING) |
| uv_build | backend build backend | MIT / Apache-2.0 | M0 | 2026-11-11 (with F-DEV-TOOLING) |
| @tanstack/react-table, react-virtual | tag grid | MIT | M1 | |
| watchfiles | folder triggers | MIT | M2 | |
| apprise | notifications | BSD-2-Clause | M2 | |
| mutagen | FilesBackend | GPL-2.0-or-later | later | |
| UI component library | UI | TBD (F-UI-SHELL open question) | M0 | |

## M0 — Skeleton

**Done when:** a two-step workflow (`beets.import` in quiet mode, then a
`shell` step) runs from the UI and from the CLI against a temporary profile,
with the run log streaming live. Covered by one Playwright test and one CLI
integration test.

Implementation order (each depends on the ones above it):

| # | Spec | What it delivers |
|---|---|---|
| 1 ✓ | [F-DEV-TOOLING](../specs/features/F-DEV-TOOLING.md) | backend and frontend project skeletons, checks, CI, audio fixtures |
| 2 ✓ | [C-WORKFLOW](../specs/core/C-WORKFLOW.md) | workflow YAML models, validation, `{{ }}` interpolation, schema export |
| 3 ✓ | [C-BACKEND](../specs/core/C-BACKEND.md) | `LibraryBackend` protocol, snapshots, capabilities, `FakeBackend` |
| 4 ✓ | [C-PROFILE](../specs/core/C-PROFILE.md) | profile files, variables, credential references |
| 5 ✓ | [C-RUN](../specs/core/C-RUN.md) | run state machine, step registry, events, cancellation |
| 6 ✓ | [S-SHELL](../specs/steps/S-SHELL.md) | `shell` step (`emits: none` only) |
| 7 ✓ | [F-WORKER](../specs/features/F-WORKER.md) | worker process per profile, server ↔ worker channel |
| 8 | [S-BEETS-IMPORT](../specs/steps/S-BEETS-IMPORT.md) | `BeetsBackend` (first slice) and `beets.import` quiet mode, contract tests |
| 9 | [F-SECURITY](../specs/features/F-SECURITY.md) | Host/Origin/JSON checks, local token, non-localhost refusal |
| 10 | [F-API](../specs/features/F-API.md) | HTTP routes, SSE event stream, app DB |
| 11 | [F-CLI](../specs/features/F-CLI.md) | `rifferr serve`, `run`, `validate` |
| 12 | [F-UI-SHELL](../specs/features/F-UI-SHELL.md) | app shell, workflow list, start a run, live run log |

**Main risks:**
- beets in-process configuration and plugin loading uses semi-internal APIs.
  Isolate it in one module and pin it with contract tests from day one.
- ~~Server ↔ worker channel choice~~ decided 2026-11-11: inherited pipes (F-WORKER).
- Security checks must exist before the first mutating endpoint, not after.

## M1 — ChangeSets and tag scripts

**Done when:** the release-type suffix logic from the reference checklist runs
as a `tags.script` with live preview and an idempotence check, and its
ChangeSets can be reviewed, applied and undone.

Order: C-CHANGESET → C-JOURNAL → S-SHELL update (`emits: changes`) →
S-TAGSCRIPT → S-REVIEW → S-BEETS-COMMAND → F-TAG-GRID → F-REVIEW-QUEUE.

## M2 — Unattended imports

**Done when:** music dropped in the inbox imports unattended; an ambiguous
album waits in review, sends a notification and doesn't block other albums.

Order: F-TRIGGERS (watch, schedule, webhook, dedup, concurrency) → lanes in
C-RUN → F-IMPORT-UI (interactive decisions) → S-NOTIFY → S-FILES-CLEANUP →
S-SUBWORKFLOW → queue dashboard (F-REVIEW-QUEUE update).

## M3 — Cover search

**Done when:** the user can pick art from at least three providers with weights.

Order: S-COVERS-SEARCH (provider protocol, ranking, cache, first providers) →
F-COVER-PICKER.

## M4 — AI

**Done when:** a genre step proposes vocabulary-valid values into review.

Order: S-AI-TAG (providers, validation, cache) → F-AI-CHAT.

## M5 — Reference workflow and packaging

**Done when:** `examples/workflows/foobar-style.yaml` reproduces the original
checklist, every checklist line is mapped in `docs/coverage.md`, and Rifferr
installs from PyPI and runs from the Docker image.

Order: reference workflow + coverage → F-PACKAGING (PyPI with bundled
frontend, Docker image with PUID/PGID).

M1–M5 stay at headline level here. Each gets detailed, with draft specs, when
it starts.
