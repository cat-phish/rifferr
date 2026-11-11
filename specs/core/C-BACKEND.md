# C-BACKEND: LibraryBackend protocol, snapshots and FakeBackend
Status: Draft
Milestone: M0

## Context
The engine, steps, UI, covers and AI only ever see a library through the
`LibraryBackend` protocol and plain snapshot data (architecture tenet 2, §8;
AGENTS.md non-negotiable 2). This spec defines that protocol in
`rifferr.backends.base`, the snapshot models in `rifferr.core`, and an in-memory
`FakeBackend` used by all engine and step tests. `BeetsBackend` is specified in
S-BEETS-IMPORT (M0 slice) and later specs.

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
                      decide: DecisionCallback | None) -> ImportResult: ...
    def command_env(self) -> Mapping[str, str]: ...
```

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
- How do run markers work across backends? Architecture uses beets flexible attribute `rifferr_run`; is "albums touched by this run" a backend responsibility (`Selection.run_id`) or tracked by the engine in the app DB?
- Should `FieldValue` allow dates as a distinct type, or keep them as strings as beets does?
- Is the backend entry-point group (`rifferr.backends`) wanted in M0, or only once a second backend exists?

## Tasks
- [ ] snapshot + selection models in core
- [ ] protocol + capabilities in backends/base.py
- [ ] FakeBackend with query subset
- [ ] backend contract test suite
- [ ] snapshot JSON Schema export
