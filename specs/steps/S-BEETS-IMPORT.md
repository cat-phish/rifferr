# S-BEETS-IMPORT: `beets.import` step (quiet mode) and first BeetsBackend slice
Status: Draft
Milestone: M0

## Context
`beets.import` imports a folder into the profile's beets library (architecture
§3). Import is one of the few places beets is used in-process, because the CLI
asks import questions as terminal prompts (§8 "BeetsBackend"). M0 implements
`mode: quiet` only, plus the parts of `BeetsBackend` needed for it and for
`query`/`evaluate`/`matches`. `auto` and `interactive` arrive with
F-IMPORT-UI in M2. Prior art: beets-flask's `ImportSession` subclass (MIT).

## Behavior
- BEETS-IMPORT-1: `rifferr.backends.beets` MUST be the only package that imports `beets` (enforced by DEV-TOOLING-4), and MUST load beets configuration and plugins from the profile's `beetsdir` inside the worker process only.
- BEETS-IMPORT-2: Config and plugin loading MUST live in one module (`backends/beets/config.py`) and be covered by contract tests, because it uses semi-internal beets functions.
- BEETS-IMPORT-3: The step's `with.path` (templated) MUST be an existing directory; otherwise the step fails with a clear message before beets is called.
- BEETS-IMPORT-4: `mode: quiet` MUST import using the user's beets importer settings with quiet behavior (`quiet_fallback` as configured by the user), never prompting.
- BEETS-IMPORT-5: Every album imported by the step MUST get the flexible attribute `rifferr_run=<run_id>`, so later steps' default selection finds it (BACKEND-5).
- BEETS-IMPORT-6: The step MUST emit one `run.step.log` event per album decision (imported, skipped with reason, duplicate) and finish with counts in `run.step.finished` data.
- BEETS-IMPORT-7: Albums beets skips in quiet mode MUST be listed in the step result as `skipped` with path and reason; they MUST NOT fail the step.
- BEETS-IMPORT-8: `with.mode` values `auto` and `interactive` MUST be validation errors in M0, naming M2.
- BEETS-IMPORT-9: `BeetsBackend.query`, `evaluate` and `matches` MUST use beets' own query parser and `evaluate_template`, so conditions and templates behave exactly as in beets, including plugin template functions.
- BEETS-IMPORT-10: `BeetsBackend.command_env` MUST return `BEETSDIR` for the profile, so `beet` subprocesses use the same config.
- BEETS-IMPORT-11: `BeetsBackend` MUST pass the backend contract suite (BACKEND-8) for the capabilities it declares.
- BEETS-IMPORT-12: Contract tests MUST pin, against a temp profile under `tmp_path` and generated audio fixtures: quiet import of a tagged fixture album, flexible attribute storage, `evaluate_template` on `$albumartist - $album`, and a query with `::` regex. They run first on any beets version bump.

## Interfaces
```python
class BeetsImportParams(BaseModel):
    path: str                                    # templated
    mode: Literal["quiet"] = "quiet"             # "auto" | "interactive" in M2
    set: dict[str, str] = {}                     # extra fields, passed as beets `--set`
    # beets importer options (copy/move/write, autotag) come from the user's beets config
```
```
backends/beets/
  __init__.py      # BeetsBackend
  config.py        # config + plugin loading (contract-tested)
  importer.py      # ImportSession subclass (quiet now; decisions in M2)
  snapshots.py     # Item/Album -> snapshot conversion
```

## Examples
```yaml
- id: import
  uses: beets.import
  with: { path: "{{ vars.inbox }}", mode: quiet }
```
Inbox with one well-tagged fixture album and one empty folder → step succeeds;
1 imported (has `rifferr_run`), 1 skipped (`no audio files`).

## Extensibility
beets plugins configured by the user load as usual and affect import and templates. The step's own options are fixed.

## Security & safety
- Uses the user's beets config read-only; Rifferr never writes to the beets config file.
- Contract tests never touch the developer's real beets config (DEV-TOOLING-6).

## Non-goals
- `auto` / `interactive` modes and the review queue for import decisions (F-IMPORT-UI, M2).
- `BeetsBackend.apply` (C-CHANGESET, M1).

## Open questions
- Should beets importer options (copy vs move, write, autotag on/off) be overridable per step, or always come from the user's beets config? Architecture is silent.
- Is `set:` (beets `--set`) wanted in M0?
- How should the worker release its `Library` handle around `beets.command` subprocesses (architecture §8) — decide now or in S-BEETS-COMMAND (M1)?
- Which beets version range exactly: `>=2.14,<2.15`?

## Tasks
- [ ] config + plugin loading module and contract tests
- [ ] snapshot conversion
- [ ] query/evaluate/matches via beets
- [ ] quiet ImportSession + rifferr_run tagging
- [ ] step type registered via entry point
- [ ] contract suite run against BeetsBackend
