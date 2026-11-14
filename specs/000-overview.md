# OVERVIEW: Rifferr overview and spec index
Status: Draft
Milestone: M0

## Context
Derived from [docs/architecture.md](../docs/architecture.md). The architecture
doc is the seed; each section becomes, or is refined by, a spec below. When a
spec and the architecture doc disagree, the Approved spec wins and the
architecture doc is updated to match.

## Conventions
- Spec files: `specs/core/C-<NAME>.md`, `specs/steps/S-<NAME>.md`,
  `specs/features/F-<NAME>.md`. The first line is `# <ID>: Title`, where `<ID>`
  equals the filename stem.
- Behavior IDs drop the category prefix: spec `S-TAGSCRIPT` has behaviors
  `TAGSCRIPT-1`, `TAGSCRIPT-2`, … Each is written at the start of a list item
  in `## Behavior` (`- TAGSCRIPT-4: The evaluator MUST …`).
- Tests reference behaviors with underscores: `test_TAGSCRIPT_4_replace_regex_multivalue`,
  so `uv run pytest -k TAGSCRIPT_4` selects them.
- Only humans set Status to Approved.
- Lookup and checks: `python3 scripts/spec.py --help`.

## Spec index
A row gets a link once its spec file exists. Build order: [docs/roadmap.md](../docs/roadmap.md).

| ID | Title | Arch § | Milestone | Status |
|---|---|---|---|---|
| [C-PROFILE](core/C-PROFILE.md) | Profiles, variables, credential references | 2 | M0 | Implemented |
| [C-WORKFLOW](core/C-WORKFLOW.md) | Workflow YAML schema, steps, interpolation | 2 | M0 | Implemented |
| [C-BACKEND](core/C-BACKEND.md) | LibraryBackend protocol, snapshots, capabilities | 8 | M0 | Implemented |
| [C-RUN](core/C-RUN.md) | Run state machine, lanes, events | 2, 2a | M0 | Implemented |
| [C-CHANGESET](core/C-CHANGESET.md) | ChangeSet model and apply/review policy | 2 | M1 | Approved |
| [C-JOURNAL](core/C-JOURNAL.md) | Journal and undo | 2 | M1 | Approved |
| [S-BEETS-IMPORT](steps/S-BEETS-IMPORT.md) | `beets.import` step | 3, 8 | M0 | Implemented |
| [S-SHELL](steps/S-SHELL.md) | `shell` step (M1: `emits: changes`) | 5 | M1 | Approved |
| [S-BEETS-COMMAND](steps/S-BEETS-COMMAND.md) | `beets.command` step | 3 | M1 | Implemented |
| [S-TAGSCRIPT](steps/S-TAGSCRIPT.md) | `tags.script` step and action set | 4 | M1 | Approved |
| [S-REVIEW](steps/S-REVIEW.md) | `review` gate step | 3 | M1 | Approved |
| S-NOTIFY | `notify` step | 2a, 3 | M2 | Planned |
| S-FILES-CLEANUP | `files.cleanup` step | 3 | M2 | Planned |
| S-SUBWORKFLOW | `workflow` (call another workflow) step | 3 | M2 | Planned |
| S-COVERS-SEARCH | `covers.search` step and provider protocol | 6 | M3 | Planned |
| S-AI-TAG | `ai.tag` step | 7 | M4 | Planned |
| [F-DEV-TOOLING](features/F-DEV-TOOLING.md) | Project skeleton, checks, CI, fixtures | 9, 10 | M0 | Implemented |
| [F-SECURITY](features/F-SECURITY.md) | Host/Origin checks, local token, remote auth, workflow trust | 8 | M0 | Implemented |
| [F-API](features/F-API.md) | HTTP API and SSE event stream | 8 | M0 | Implemented |
| [F-CLI](features/F-CLI.md) | `rifferr serve`, `run`, `validate` | 9 | M0 | Implemented |
| [F-WORKER](features/F-WORKER.md) | Worker process per profile and IPC | 8 | M0 | Implemented |
| [F-UI-SHELL](features/F-UI-SHELL.md) | UI shell and run log | 11 | M0 | Implemented |
| [F-TAG-GRID](features/F-TAG-GRID.md) | Library and tag grid (script preview: S-TAGSCRIPT) | 4 | M1 | Implemented |
| [F-REVIEW-QUEUE](features/F-REVIEW-QUEUE.md) | Review queue UI and dashboard | 2a | M1 | Implemented |
| F-IMPORT-UI | Interactive import decisions | 3, 8 | M2 | Planned |
| [F-TRIGGERS](features/F-TRIGGERS.md) | watch, schedule, webhook triggers; dedup; concurrency | 2a | M2 | Approved |
| F-COVER-PICKER | Cover search UI | 6 | M3 | Planned |
| F-AI-CHAT | Chat panel and tools | 7 | M4 | Planned |
| F-PACKAGING | PyPI and Docker packaging | 8, 10 | M5 | Planned |

## Open questions
- Carried over from architecture §12 until each lands in a specific spec.
