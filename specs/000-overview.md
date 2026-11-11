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
Planned specs. A row gets a link once its file exists.

| ID | Title | Arch § | Milestone | Status |
|---|---|---|---|---|
| C-PROFILE | Profiles, variables, credential references | 2 | M0 | Planned |
| C-WORKFLOW | Workflow YAML schema, steps, interpolation | 2 | M0 | Planned |
| C-BACKEND | LibraryBackend protocol, snapshots, capabilities | 8 | M0 | Planned |
| C-RUN | Run state machine, lanes, events | 2, 2a | M0 | Planned |
| C-CHANGESET | ChangeSet model and apply/review policy | 2 | M1 | Planned |
| C-JOURNAL | Journal and undo | 2 | M1 | Planned |
| S-BEETS-IMPORT | `beets.import` step | 3, 8 | M0 | Planned |
| S-SHELL | `shell` step | 5 | M0 | Planned |
| S-BEETS-COMMAND | `beets.command` step | 3 | M1 | Planned |
| S-TAGSCRIPT | `tags.script` step and action set | 4 | M1 | Planned |
| S-REVIEW | `review` gate step | 3 | M1 | Planned |
| S-NOTIFY | `notify` step | 2a, 3 | M2 | Planned |
| S-FILES-CLEANUP | `files.cleanup` step | 3 | M2 | Planned |
| S-WORKFLOW | `workflow` (call another workflow) step | 3 | M2 | Planned |
| S-COVERS-SEARCH | `covers.search` step and provider protocol | 6 | M3 | Planned |
| S-AI-TAG | `ai.tag` step | 7 | M4 | Planned |
| F-SECURITY | Host/Origin checks, local token, remote auth, workflow trust | 8 | M0 | Planned |
| F-API | HTTP API and SSE event stream | 8 | M0 | Planned |
| F-CLI | `rifferr serve`, `run`, `validate` | 9 | M0 | Planned |
| F-WORKER | Worker process per profile and IPC | 8 | M0 | Planned |
| F-UI-SHELL | UI shell and run log | 11 | M0 | Planned |
| F-TAG-GRID | Library/tag grid and script preview UI | 4 | M1 | Planned |
| F-REVIEW-QUEUE | Review queue UI and dashboard | 2a | M1 | Planned |
| F-IMPORT-UI | Interactive import decisions | 3, 8 | M2 | Planned |
| F-TRIGGERS | watch, schedule, webhook triggers; dedup; concurrency | 2a | M2 | Planned |
| F-COVER-PICKER | Cover search UI | 6 | M3 | Planned |
| F-AI-CHAT | Chat panel and tools | 7 | M4 | Planned |
| F-PACKAGING | PyPI and Docker packaging | 8, 10 | M5 | Planned |

## Open questions
- Carried over from architecture §12 until each lands in a specific spec.
