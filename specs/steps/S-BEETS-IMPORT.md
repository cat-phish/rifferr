# S-BEETS-IMPORT: `beets.import` step (quiet mode) and first BeetsBackend slice
Status: Implemented
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
- BEETS-IMPORT-4: `mode: quiet` MUST import using the user's beets importer settings with quiet behavior (`quiet_fallback` as configured by the user), never prompting. `with.options` MAY override these importer options for this import only: `copy`, `move`, `link`, `hardlink`, `reflink`, `write`, `autotag`, `quiet_fallback`, `duplicate_action`, `incremental`. Any other key MUST be a validation error. Overrides MUST apply in memory and never be written to the beets config.
- BEETS-IMPORT-5: Every album imported by the step MUST get the flexible attribute `rifferr_run=<run_id>`, so later steps' default selection finds it (BACKEND-5).
- BEETS-IMPORT-6: The step MUST emit one `run.step.log` event per album decision (imported, skipped with reason, duplicate) and finish with outputs `imported`, `skipped` and `duplicates` (counts).
- BEETS-IMPORT-7: Each album beets skips in quiet mode MUST produce a `run.step.import.skipped` event with `path` and `reason`; skips MUST NOT fail the step.
- BEETS-IMPORT-8: `with.mode` values `auto` and `interactive` MUST be validation errors in M0, naming M2.
- BEETS-IMPORT-9: `BeetsBackend.query`, `evaluate` and `matches` MUST use beets' own query parser and `evaluate_template`, so conditions and templates behave exactly as in beets, including plugin template functions.
- BEETS-IMPORT-10: `BeetsBackend.command_env` MUST return `BEETSDIR` for the profile, so `beet` subprocesses use the same config.
- BEETS-IMPORT-11: `BeetsBackend` MUST pass the backend contract suite (BACKEND-8) for the capabilities it declares.
- BEETS-IMPORT-13: `with.set` values (after `{{ }}` interpolation, no beets templates) MUST be applied to imported albums through beets' `import.set_fields`.
- BEETS-IMPORT-14: `BeetsBackend` MUST open the beets `Library` for each backend call and close it afterwards, never holding it between calls, so `beet` subprocesses started by other steps don't compete for SQLite write locks.
- BEETS-IMPORT-15: The package MUST register a `rifferr.backends` entry point `beets` whose factory refuses a profile without `beets.beetsdir` (PROFILE-8).
- BEETS-IMPORT-16: The import MUST run in a thread so the worker's event loop stays responsive; cancellation (RUN-8) MUST take effect between albums.
- BEETS-IMPORT-12: Contract tests MUST pin, against a temp profile under `tmp_path` and generated audio fixtures: quiet import of a tagged fixture album, flexible attribute storage, `evaluate_template` on `$albumartist - $album`, and a query with `::` regex. They run first on any beets version bump.

Note (2026-11-13, C-LANES): `beets.import` is `run_level` (LANES-6). It runs before any lane exists, and each album it imports gets a lane at the next step (LANES-1).

## Interfaces
```python
class BeetsImportParams(BaseModel):
    path: str                                    # templated
    mode: Literal["quiet"] = "quiet"             # "auto" | "interactive" in M2
    set: dict[str, str] = {}                     # extra fields, via beets import.set_fields
    options: ImporterOptions = {}                # allowlisted overrides (BEETS-IMPORT-4)
    # everything else comes from the user's beets config
```
```
backends/beets/
  __init__.py      # BeetsBackend
  config.py        # config + plugin loading (contract-tested)
  importer.py      # ImportSession subclass (quiet now; decisions in M2)
  snapshots.py     # Item/Album -> snapshot conversion
```

As implemented:
- `rifferr.backends.beets`: `BeetsBackend`, `from_profile` (entry point), `escape_template`;
  `config.py` (`load`, `overlay`, `reset` for tests, `library_paths`); `importer.py`
  (`QuietImportSession`); `snapshots.py`. Snapshot ids are `album-<id>` / `item-<id>`.
- Step: `rifferr.steps.beets_import.BeetsImportStep`; `ImporterOptions` holds the allowlist
  (`duplicate_action` also accepts beets 2.14's `upgrade`; `ask` is not offered).
- The import overlays `import.quiet: yes` and `import.threaded: no` (so stop checks and
  decisions happen one album at a time), plus `set_fields` with `rifferr_run` and `with.set`.
  `with.set` values are escaped (`$ % } ,` get a `$`) so beets keeps them literal.
- Quiet decisions: strong match → apply, otherwise `quiet_fallback`; duplicate with
  `duplicate_action: ask` → skip; resume prompts → start over.
- Skip reasons: `no strong match (quiet_fallback: skip)`, `duplicate of an album in the
  library`, `already imported (incremental)`. Duplicate reasons are the action taken
  (`skip`, `keep`, `remove`, `merge`, `upgrade`).
- "imported" decisions are reported after the import finishes; skips and duplicates as they
  happen. Run marker term: `rifferr_run::^<run id>$`.
- Without `run_id`, `import_folder` still marks albums with a fresh id so it can list them.

## Examples
```yaml
- id: import
  uses: beets.import
  with: { path: "{{ vars.inbox }}", mode: quiet }
```
Inbox with one well-tagged fixture album → step succeeds; 1 imported (has `rifferr_run`).
Skip reasons are whatever beets reports; the contract tests record them.

## Extensibility
beets plugins configured by the user load as usual and affect import and templates. The step's own options are fixed.

## Security & safety
- Uses the user's beets config read-only; Rifferr never writes to the beets config file.
- Contract tests never touch the developer's real beets config (DEV-TOOLING-6), and set `autotag: no` (or replay recorded MusicBrainz responses) so they make no network calls.

## Non-goals
- `auto` / `interactive` modes and the review queue for import decisions (F-IMPORT-UI, M2).
- `BeetsBackend.apply` (C-CHANGESET, M1).

## Open questions
- ~~Importer options per step?~~ Resolved 2026-11-11 (author): allowlisted overrides (BEETS-IMPORT-4).
- ~~`set:` in M0?~~ Resolved 2026-11-11 (author): yes, plain strings (BEETS-IMPORT-13).
- ~~Library handle?~~ Resolved 2026-11-11 (author): open per call (BEETS-IMPORT-14); S-BEETS-COMMAND may refine.
- ~~Version range?~~ Resolved 2026-11-11 (author): `>=2.14,<2.15`; bumps start with the contract tests.

## Tasks
- [x] config + plugin loading module and contract tests
- [x] snapshot conversion
- [x] query/evaluate/matches via beets
- [x] quiet ImportSession + rifferr_run tagging
- [x] step type registered via entry point
- [x] contract suite run against BeetsBackend
