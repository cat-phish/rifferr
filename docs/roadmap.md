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
| `docs/original-checklist.md` filled in by the author | M1 (release-type suffix logic), M5 | Present (2026-11-13) |
| Decisions in M0 specs' *Open questions* | M0 approval | Open |

## Dependency ledger

Every runtime or dev dependency needs the author's OK before it is added
(AGENTS.md). Licenses must be AGPL-compatible.

| Package | Use | License | Milestone | Approved |
|---|---|---|---|---|
| beets (`>=2.14,<2.15`) | library backend | MIT; transitive deps MIT/BSD/Apache-2.0/PSF/MPL-2.0, Unidecode and mutagen GPL-2.0-or-later (checked 2026-11-11) | M0 | 2026-11-11 |
| fastapi (`>=0.162,<1`) | API server | MIT; transitive deps starlette BSD-3-Clause, anyio MIT, annotated-doc MIT, opentelemetry-api Apache-2.0 (checked 2026-11-11) | M0 | 2026-11-11 |
| uvicorn (`>=0.54,<1`) | ASGI server | BSD-3-Clause; transitive deps click BSD-3-Clause, h11 MIT (checked 2026-11-11) | M0 | 2026-11-11 |
| httpx (`>=0.28,<1`) | CLI HTTP client | BSD-3-Clause; transitive deps httpcore BSD-3-Clause, certifi MPL-2.0, idna BSD-3-Clause, h11 MIT, anyio MIT (checked 2026-11-11) | M0 | 2026-11-11 |
| pydantic v2 | models, JSON Schema | MIT | M0 | 2026-11-11 |
| ruamel.yaml | round-trip YAML | MIT | M0 | 2026-11-11 |
| keyring | secrets | MIT | M0 | 2026-11-11 |
| httpx | HTTP client | BSD-3-Clause | M0 | |
| pytest, pytest-asyncio | tests | MIT / Apache-2.0 | M0 | 2026-11-11 |
| hypothesis | property tests (dev only) | MPL-2.0 | M0 | 2026-11-11 |
| respx | HTTP mocks (dev only) | BSD-3-Clause | M0 | 2026-11-11 |
| ruff, pyright | lint, types (dev only) | MIT | M0 | 2026-11-11 |
| react, react-dom, vite, typescript | frontend | MIT / Apache-2.0 | M0 | 2026-11-11 |
| @mantine/core, @mantine/hooks (9.x) | UI components | MIT; production transitive deps all MIT except tslib 0BSD and type-fest MIT OR CC0-1.0 (checked 2026-11-11) | M0 | 2026-11-11 |
| react-router (8.x) | routing | MIT | M0 | 2026-11-11 |
| @tanstack/react-query (5.x), @tanstack/react-virtual (3.x) | REST cache, virtualized log | MIT | M0 | 2026-11-11 |
| openapi-fetch (0.17) | typed API client | MIT | M0 | 2026-11-11 |
| @playwright/test (1.x, dev) | end-to-end tests | Apache-2.0 | M0 | 2026-11-11 |
| @tanstack/react-query | data fetching | MIT | M0 | |
| openapi-typescript | API client types | MIT | M0 | 2026-11-11 |
| vitest, @testing-library/react | frontend tests | MIT | M0 | 2026-11-11 |
| @playwright/test | e2e | Apache-2.0 | M0 | |
| eslint, prettier | frontend lint | MIT | M0 | 2026-11-11 |
| @vitejs/plugin-react, @types/react, @types/react-dom, @types/node, @eslint/js, typescript-eslint, eslint-plugin-react-hooks, globals, @testing-library/jest-dom, jsdom | companions of the approved frontend tools | MIT | M0 | 2026-11-11 (with F-DEV-TOOLING) |
| uv_build | backend build backend | MIT / Apache-2.0 | M0 | 2026-11-11 (with F-DEV-TOOLING) |
| @tanstack/react-virtual | tag grid, run log (react-table not added: F-TAG-GRID, 2026-11-12) | MIT | M1 | |
| watchfiles | folder triggers | MIT | M2 | 2026-11-13 (F-TRIGGERS) |
| cronsim | schedule triggers (cron next-fire with DST) | BSD-3-Clause | M2 | 2026-11-13 (F-TRIGGERS) |
| apprise | notifications | BSD-2-Clause | M2 | |
| mutagen | FilesBackend | GPL-2.0-or-later | later | |
| UI component library | UI | TBD (F-UI-SHELL open question) | M0 | |

## M0 — Skeleton

**Done when:** a two-step workflow (`beets.import` in quiet mode, then a
`shell` step) runs from the UI and from the CLI against a temporary profile,
with the run log streaming live. Covered by one Playwright test and one CLI
integration test. **Met 2026-11-11:** `frontend/e2e/two-step.spec.ts` and
`test_CLI_8_example_two_step_workflow_end_to_end`, both running
`examples/workflows/two-step.yaml`.

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
| 8 ✓ | [S-BEETS-IMPORT](../specs/steps/S-BEETS-IMPORT.md) | `BeetsBackend` (first slice) and `beets.import` quiet mode, contract tests |
| 9 ✓ | [F-SECURITY](../specs/features/F-SECURITY.md) | Host/Origin/JSON checks, local token, non-localhost refusal |
| 10 ✓ | [F-API](../specs/features/F-API.md) | HTTP routes, SSE event stream, app DB |
| 11 ✓ | [F-CLI](../specs/features/F-CLI.md) | `rifferr serve`, `run`, `validate` |
| 12 ✓ | [F-UI-SHELL](../specs/features/F-UI-SHELL.md) | app shell, workflow list, start a run, live run log |

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

Progress: C-CHANGESET, C-JOURNAL, the S-SHELL update and S-TAGSCRIPT Implemented
2026-11-12, including RUN-14 suspension on ChangeSet holds. S-REVIEW Implemented
2026-11-12 (review gates as RUN-14 holds, timeouts, reject with undo).
The done-when still needs the reference release-type suffix script, which waits for
`docs/original-checklist.md` (S-TAGSCRIPT Open questions). S-BEETS-COMMAND Implemented
2026-11-12. F-TAG-GRID Implemented 2026-11-12 (library browser, manual edits as
ChangeSets, profile event stream, undo of manual edits). F-REVIEW-QUEUE Implemented
2026-11-13 (queue API and `/review` page, partial accept, re-propose after a conflict,
gate decisions, run undo from the run page). The reference release-type scripts
(`examples/scripts/release-types.yaml`, `compilation.yaml`) and the first
`examples/workflows/foobar-style.yaml` landed 2026-11-13, tested on the fake backend
and real beets, with `docs/coverage.md` mapping every checklist line.
**M1 done 2026-11-13.**

### To verify on a full dev machine

The C-CHANGESET, C-JOURNAL, S-SHELL update and S-TAGSCRIPT work (2026-11-12, commits
`0169123`..`aa9c0df`) was built on a NixOS VM that couldn't run everything. Before
pushing, run these on a normal desktop and tick them off:

- [x] `cd backend && uv run pytest`: everything passes. On the VM, 8 tests in
  `tests/steps/test_shell.py` failed because `/usr/bin` has only `env` there (B-032):
  SHELL_1 shell string, SHELL_3 per album / per item / per run, SHELL_5 stdin ignored,
  SHELL_6 redaction, SHELL_10 cwd defaults / templated. They must pass here.
  2026-11-12, Fedora desktop: 952 passed, 52 skipped (capability-gated contract
  tests and the ffmpeg fixture test, B-012); the shell tests pass.
- [x] `cd frontend && pnpm e2e`: neither Playwright spec has run on the VM (no
  browser installed).
  - `e2e/two-step.spec.ts` (UI_SHELL_13): still passes. Global setup now pre-imports
    a copy of the fixture album as "Preview Sessions", so check the two-step import
    still reports "imported" and not a duplicate.
  - `e2e/tagscript.spec.ts` (TAGSCRIPT_22): new; never run. It covers live preview,
    idempotence highlight, validation line:column, save, and the script list.
  2026-11-12: both pass; the two-step log shows `[import] imported`. Run with
  `env -u LD_LIBRARY_PATH` (B-012).
- [x] `cd frontend && pnpm test`: still fails because vitest also collects
  `e2e/*.spec.ts` (B-029). The unit tests pass with `pnpm exec vitest run src` (21).
  Fix B-029, or confirm only the e2e files fail.
  2026-11-12: only the e2e files failed; B-029 fixed, 21 passed.
- [x] Re-run the timing-sensitive tests a few times under load:
  `test_WORKER_5_cancel_a_queued_run` and `test_API_6_queued_run_shows_pending_steps`
  (B-031, cancel before the engine exists), and the read-only worker timing tests in
  `tests/worker/test_readonly.py` and `tests/api/test_scripts.py`
  (TAGSCRIPT_21 timeout / superseded, WORKER_11 crash).
  2026-11-12: 5 runs alongside 2 full suites, 22/22 each time.
- [x] `pre-commit run --all-files` (gitleaks + spec lint). It isn't installed on the VM.
  2026-11-12: all hooks pass.
- [x] Manual check with `uv run rifferr serve --dev` against a test profile (never a
  real library): open `/scripts`, edit a script, and confirm the preview answers while
  a run is importing (read-only worker, WORKER-11).
  2026-11-12: the preview answered while a run was active (temp profile; the run
  imported, then held the main worker with a 90 s shell step).
- [x] Then commit any fixes, push, and confirm CI is green (B-014).
  2026-11-12: green on `d3062c9`, after fixing the py3.12 OpenAPI drift (422 description).

## M2 — Unattended imports

**Done when:** music dropped in the inbox imports unattended; an ambiguous
album waits in review, sends a notification and doesn't block other albums.

Order: F-TRIGGERS (watch, schedule, webhook, dedup, concurrency) → lanes
(C-LANES) → F-IMPORT-UI (interactive decisions) → S-NOTIFY → S-FILES-CLEANUP →
S-SUBWORKFLOW → queue dashboard (F-REVIEW-QUEUE update).

- [x] F-TRIGGERS: Implemented 2026-11-13.
- [x] C-LANES: Implemented 2026-11-13.
- [x] F-IMPORT-UI: Implemented 2026-11-13.

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
