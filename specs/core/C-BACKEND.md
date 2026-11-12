# C-BACKEND: LibraryBackend protocol, snapshots and FakeBackend
Status: Implemented
Milestone: M0

## Context
The engine, steps, UI, covers and AI only ever see a library through the
`LibraryBackend` protocol and plain snapshot data (architecture tenet 2, §8;
AGENTS.md non-negotiable 2). This spec defines that protocol in
`rifferr.backends.base`, the snapshot models in `rifferr.core`, and an in-memory
`FakeBackend` used by all engine and step tests. `BeetsBackend` is specified in
S-BEETS-IMPORT (M0 slice) and later specs.


M1 additions (BACKEND-10 to -13) are specified in [S-TAGSCRIPT](../steps/S-TAGSCRIPT.md) (approved 2026-11-11) and implemented with it.
## Behavior
- BACKEND-1: `rifferr.backends.base` MUST define `LibraryBackend` as a `typing.Protocol` with `capabilities`, `query`, `evaluate`, `matches`, `apply`, `import_folder` and `command_env`, as in architecture §8.
- BACKEND-2: `BackendCapabilities` MUST be a frozen model of booleans (`import_`, `query`, `templates`, `apply`, `undo`, `command_env`); a backend MUST raise `CapabilityError` when a method it doesn't support is called.
- BACKEND-3: `AlbumSnapshot` and `ItemSnapshot` MUST be immutable, JSON-serializable Pydantic models carrying a backend-assigned opaque `id`, a `path`, and `fields: dict[str, FieldValue]` where `FieldValue` is `str | int | float | bool | None | list[str]`.
- BACKEND-4: An `AlbumSnapshot` MUST contain its `items` in track order, and every snapshot MUST be serializable to JSON and back without loss (the same JSON is the `shell` step's stdin, architecture §5).
- BACKEND-5: `Selection` MUST describe which albums/items to act on as a backend query string plus a scope (`album` or `item`); the default selection for a step is "albums touched by this run".
- BACKEND-6: `FakeBackend` MUST implement the full protocol in memory, with `query`/`matches` supporting a documented subset of beets query syntax (`field:value`, `field::regex`, negation `^`, and space-separated AND) and `evaluate` supporting `$field` substitution.
- BACKEND-7: `FakeBackend.import_folder` MUST create albums from a declarative fixture description (no files read), and record each call so tests can assert on it.
- BACKEND-8: A shared backend contract test suite MUST exist that any backend implementation can run against itself; `FakeBackend` MUST pass it, and S-BEETS-IMPORT MUST run the same suite against `BeetsBackend` for the capabilities it offers.
- BACKEND-9: `apply` and the `ChangeSet`/`JournalEntry` types MUST be declared in the protocol now but `FakeBackend.apply` MAY raise `NotImplementedError` until C-CHANGESET (M1).
- BACKEND-10: `evaluate` and `matches` MUST use the field values in the snapshot they are given, not the stored ones, so they work on working values. Fields absent from the snapshot fall back to the stored object; a field given as `None` counts as absent (added for S-TAGSCRIPT).
- BACKEND-11: `coerce(field, value) -> FieldValue` MUST convert a value to the field's type (beets: the field's type; flexible attributes stay strings) and raise `ValueError` with a message when it can't (added for S-TAGSCRIPT).
- BACKEND-12: `album_item_fields()` MUST return the album fields the backend copies to items on store (beets: `Album.item_keys` and every flexible attribute), as a `SharedFields` set. `FakeBackend` returns its configured set (added for S-TAGSCRIPT).
- BACKEND-13: A backend MUST be openable read-only (`from_profile(…, read_only=True)`). Read-only, it reports `apply` and `import_` as unsupported, and `BeetsBackend` sets `PRAGMA query_only` on its connection, so no write can happen. A contract test pins that writes fail (added for S-TAGSCRIPT).

## Interfaces
```python
FieldValue = str | int | float | bool | None | list[str]

class ItemSnapshot(BaseModel, frozen=True):
    id: str
    path: str
    fields: dict[str, FieldValue]

class AlbumSnapshot(BaseModel, frozen=True):
    id: str
    path: str | None
    fields: dict[str, FieldValue]
    items: list[ItemSnapshot]

class Selection(BaseModel, frozen=True):
    query: str = ""                       # backend condition syntax
    scope: Literal["album", "item"] = "album"
    run_id: str | None = None             # restrict to albums touched by this run

ImportMode = Literal["quiet", "auto", "interactive"]

class ImportResult(BaseModel):
    imported: list[AlbumSnapshot]
    skipped: list[SkippedFolder]          # path + reason

class LibraryBackend(Protocol):
    capabilities: BackendCapabilities
    def query(self, selection: Selection) -> list[AlbumSnapshot]: ...
    def evaluate(self, template: str, target: ItemSnapshot | AlbumSnapshot) -> str: ...
    def matches(self, condition: str, target: ItemSnapshot | AlbumSnapshot) -> bool: ...
    def apply(self, changes: ChangeSet) -> JournalEntry: ...
    def import_folder(self, path: Path, mode: ImportMode,
                      decide: DecisionCallback | None,
                      *, run_id: str | None = None) -> ImportResult: ...
    def command_env(self) -> Mapping[str, str]: ...
```

As implemented:
- Snapshots and `Selection` live in `rifferr.core.snapshot` / `rifferr.core.selection`; the
  protocol, `CapabilityError`, `QueryError`, `TemplateError`, `ImportResult` and
  `RUN_MARKER = "rifferr_run"` in `rifferr.backends.base`; `FakeBackend` in
  `rifferr.backends.fake` (its docstring lists the supported query subset).
- `import_folder` takes `run_id`; imported albums get `RUN_MARKER = run_id`.
- `scope="item"` returns albums that have a matching item, with only the matching items.
- `ChangeSet` / `JournalEntry` are empty placeholders in `rifferr.core.changeset` until M1.
- Query strings are split with shell rules, as beets does: quote a regex that contains a
  backslash (`'album::\bEP\b'`).
- Published schemas: `schemas/album-snapshot.schema.json`, `schemas/item-snapshot.schema.json`.
- Contract suite: `backend/tests/backends/contract.py` (`BackendContract`).
- Extended for S-BEETS-IMPORT: `import_folder(..., options=, set_fields=, should_stop=,
  on_decision=)`; `ImportDecision(path, outcome, reason)`; `ImportResult.duplicates`.
- The standard contract library uses `genres` (a list): beets 2.14 deprecates `genre`.
- Loading by name (F-WORKER): `rifferr.backends.loader.load_backend(profile)` finds the
  `rifferr.backends` entry point named by `profile.backend`; it names a factory
  `(Profile) -> LibraryBackend`.

## Examples
```python
fb = FakeBackend.from_fixture({"albums": [{"albumartist": "A", "album": "B", "albumtype": "ep",
                                           "items": [{"title": "x", "track": 1}]}]})
fb.query(Selection(query="albumtype:ep"))          # -> [AlbumSnapshot(...)]
fb.evaluate("$albumartist - $album", album)         # -> "A - B"
```

## Extensibility
Third-party backends implement the protocol and register under an entry point group `rifferr.backends`, and must pass the contract suite (BACKEND-8). The snapshot shape is fixed and public (it is published as JSON Schema for shell scripts).

## Security & safety
Snapshots carry tag data and paths only, never credentials or config.

## Non-goals
- ChangeSet semantics and undo (C-CHANGESET, C-JOURNAL, M1).
- FilesBackend (later milestone).
- Interactive import decisions (F-IMPORT-UI, M2): `decide` is accepted but only `quiet` is implemented in M0.

## Open questions
- ~~Who tracks "albums touched by this run"?~~ Resolved 2026-11-11 (author): the backend, via `Selection.run_id`. Backends store the run marker as the flexible attribute `rifferr_run` (beets) or its equivalent, set by `import_folder(..., run_id=...)`, and filter on it.
- ~~Dates as a distinct `FieldValue` type?~~ Resolved 2026-11-11 (author): no; strings and ints as beets stores them.
- ~~`rifferr.backends` entry-point group in M0?~~ Resolved 2026-11-11 (author): the group name is public now; BeetsBackend registers there in S-BEETS-IMPORT; loading a backend by name arrives with F-WORKER.

## Tasks
- [x] snapshot + selection models in core
- [x] `BackendCapabilities` in backends/base.py (landed with C-WORKFLOW)
- [x] protocol in backends/base.py
- [x] FakeBackend with query subset
- [x] backend contract test suite
- [x] snapshot JSON Schema export
