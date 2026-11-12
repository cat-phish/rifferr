# C-JOURNAL: Journal and undo
Status: Draft
Milestone: M1

## Context
The journal is the append-only record of applied ChangeSets with their old
values; undo replays it backwards (architecture §2, tenet 3; AGENTS.md
non-negotiable 3: the engine writes the journal *before* changing anything).
C-CHANGESET defines ChangeSets and `apply`; this spec defines what is recorded,
when, how undo works, and how an interrupted apply is recovered.

Author decision (2026-11-11): undo stops on conflict, like apply. If a field
changed again after the apply, nothing in that entry is undone and the
conflict is shown.

## Behavior
- JOURNAL-1: Before calling `LibraryBackend.apply`, the engine MUST append a journal entry in state `pending` holding the ChangeSet id (and accepted subset), the profile and the intended field changes with their expected old values. Only after that entry is durably committed may `apply` run.
- JOURNAL-2: After `apply` returns, the engine MUST complete the entry: `applied` with every effect (`target`, `field`, `before`, `after`, including fields the backend changed by propagation, CHANGESET-11) and every file write failure; or `failed` with the conflicts or error, if nothing was applied. Entries are never deleted or edited after that, except for the `undone_by` link (JOURNAL-5).
- JOURNAL-3: On worker start-up, the engine MUST reconcile every `pending` entry of its profile against the library. If all intended values are current, the entry becomes `applied` (effects = intended), if all old values are current it becomes `failed` ("interrupted before apply"), and anything else marks it `applied` with `uncertain: true` and puts it in review. Recovery MUST NOT write tags.
- JOURNAL-4: Undo of an entry MUST build the inverse ChangeSet (each effect's `after` → `before`, with `after` as the expected old value; `delete` for fields that didn't exist before). It is applied through the normal path (journaled first, all-or-nothing, C-CHANGESET CHANGESET-8) with `source.kind = "manual"` and `name = "undo"`.
- JOURNAL-5: If any field no longer has the entry's `after` value, the undo MUST apply nothing and report the conflicts (expected, current, would-restore). On success the original entry gets `undone_by` set to the undo's entry id and its ChangeSet becomes `undone` (C-CHANGESET CHANGESET-5). An entry that is already undone MUST NOT be undone again. Undoing the undo is a new undo of the undo's entry.
- JOURNAL-6: Undo of a run MUST undo that run's applied, not-yet-undone entries newest first, each as in JOURNAL-4/-5, stopping at the first conflict. It MUST report which entries were undone and which conflict stopped it.
- JOURNAL-7: File write failures recorded in an entry MUST be listed with the entry. After an undo, the files are rewritten from the restored values like any apply (C-CHANGESET CHANGESET-10).
- JOURNAL-8: The journal MUST live in the app DB (`journal` table, profile column), accessed only through `rifferr.appdb`; `core` and `engine` reach it through an injected `Journal` interface, so engine tests use an in-memory journal.
- JOURNAL-9: Journal entries MUST NOT contain secrets. Values are tag values. They are kept forever in M1; pruning is a later setting (like B-021).

## Interfaces
```python
JournalState = Literal["pending", "applied", "failed"]

class FieldEffect(BaseModel, frozen=True):
    target: TargetRef; field: str
    before: FieldValue; after: FieldValue
    existed_before: bool = True        # False: undo deletes the field

class WriteFailure(BaseModel, frozen=True):
    target: TargetRef; path: str; error: str

class JournalEntry(BaseModel, frozen=True):
    id: str                       # ULID
    profile: str
    changeset_id: str
    run_id: str | None
    accepted: list[int] | None    # field-change indexes when partly accepted
    state: JournalState
    intended: list[FieldChange]   # with targets, as recorded before apply
    effects: list[FieldEffect]
    write_failures: list[WriteFailure]
    conflicts: list[Conflict]
    uncertain: bool = False
    undo_of: str | None = None    # this entry undoes that one
    undone_by: str | None = None
    created_at: datetime; completed_at: datetime | None

class Journal(Protocol):           # injected into the engine
    def begin(self, entry: JournalEntry) -> None: ...      # state pending
    def complete(self, entry_id: str, result: ApplyResult) -> JournalEntry: ...
    def pending(self, profile: str) -> list[JournalEntry]: ...
    def get(self, entry_id: str) -> JournalEntry | None: ...
    def for_run(self, run_id: str) -> list[JournalEntry]: ...
    def mark_undone(self, entry_id: str, by: str) -> None: ...
```
- Undo requests arrive as worker jobs (`kind: undo`, target entry or run id), like review decisions (C-CHANGESET CHANGESET-13). Their API and UI belong to F-REVIEW-QUEUE.

## Examples
Entry E1 applied `album: "Live Sessions" → "Live Sessions (EP)"` on `album-7`;
beets also changed `album` on its 4 items → E1 effects: 5 rows.
Undo E1 → inverse ChangeSet with 5 field changes expecting "Live Sessions (EP)"
→ applied → E2 (`undo_of: E1`), E1 `undone_by: E2`.

If track 2's `album` was edited to "LS" after E1 → undo applies nothing,
conflict: `item-12 album expected "Live Sessions (EP)", current "LS", would restore "Live Sessions"`.

Worker killed between `begin` and `apply` → on restart, all old values current
→ E3 `failed: interrupted before apply`; the ChangeSet is back to `proposed`.

## Extensibility
- Backends supply effects through `apply`; the journal format is fixed.
- The `Journal` interface is replaceable in tests; production uses the app DB.

## Security & safety
- The write-ahead entry is what keeps a crash mid-apply from losing old values (non-negotiable 3).
- Recovery never writes; uncertain entries go to a human.

## Non-goals
- Undoing imports (removing albums and their files). Imports aren't ChangeSets; import undo arrives with the M2 dashboard.
- Undoing `beets.command` or `shell` side effects that bypass ChangeSets (files moved, art fetched). Those steps say so in their specs.
- A CLI `rifferr undo` (F-REVIEW-QUEUE adds the UI; a CLI command can follow).

## Open questions
- None yet.

## Tasks
- [ ] models + `Journal` protocol + in-memory journal for tests
- [ ] app DB `journal` table (migration) + `AppJournal`
- [ ] write-ahead begin / complete around apply
- [ ] start-up reconciliation of pending entries
- [ ] undo of an entry and of a run, with conflicts
- [ ] `undo` worker jobs
