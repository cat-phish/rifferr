"""Start-up reconciliation of interrupted applies (C-JOURNAL JOURNAL-3).

A ``pending`` journal entry means the worker stopped between ``begin`` and
``complete``. Recovery compares the entry's intended changes with the library
and settles the entry. It only reads the library; it never writes tags.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import JsonValue

from rifferr.backends.base import LibraryBackend
from rifferr.core.changeset import (
    ChangeSetState,
    Conflict,
    FieldChange,
    FieldEffect,
    JournalEntry,
    TargetRef,
    same_value,
)
from rifferr.core.selection import Selection
from rifferr.core.snapshot import FieldValue
from rifferr.engine.changesets import ChangeSetStore, EmitChange, SnapshotIndex
from rifferr.engine.journal import Journal

INTERRUPTED = "interrupted before apply"

#: (run id, step id) -> where that ChangeSet's events go.
EmitterFor = Callable[[str | None, str | None], EmitChange]


def _intended(entry: JournalEntry) -> list[tuple[TargetRef, FieldChange]]:
    return [(t.target, c) for t in entry.intended for c in t.changes]


def _target_value(change: FieldChange) -> FieldValue:
    return change.new if change.op == "set" else None


def reconcile(
    entry: JournalEntry,
    current: Callable[[TargetRef], dict[str, FieldValue] | None],
    now: datetime,
) -> JournalEntry:
    """The settled version of a pending entry (JOURNAL-3).

    - every intended value is current: ``applied``, effects = intended
    - every old value is current: ``failed`` ("interrupted before apply")
    - anything else: ``applied`` and ``uncertain``. Effects are the fields that
      reached their intended value; the others are recorded as conflicts.
    """
    changes = _intended(entry)

    def now_value(ref: TargetRef, field: str) -> FieldValue:
        return (current(ref) or {}).get(field)

    at_new = [same_value(now_value(r, c.field), _target_value(c)) for r, c in changes]
    at_old = [same_value(now_value(r, c.field), c.old) for r, c in changes]

    def effect(ref: TargetRef, c: FieldChange) -> FieldEffect:
        return FieldEffect(
            target=ref,
            field=c.field,
            before=c.old,
            after=_target_value(c),
            existed_before=c.old is not None,
        )

    if all(at_new):
        return entry.model_copy(
            update={
                "state": "applied",
                "effects": [effect(r, c) for r, c in changes],
                "completed_at": now,
            }
        )
    if all(at_old):
        return entry.model_copy(
            update={"state": "failed", "error": INTERRUPTED, "completed_at": now}
        )
    return entry.model_copy(
        update={
            "state": "applied",
            "uncertain": True,
            "effects": [effect(r, c) for (r, c), ok in zip(changes, at_new, strict=True) if ok],
            "conflicts": [
                Conflict(
                    target=r,
                    field=c.field,
                    expected=_target_value(c),
                    current=now_value(r, c.field),
                )
                for (r, c), ok in zip(changes, at_new, strict=True)
                if not ok
            ],
            "completed_at": now,
        }
    )


class _LibraryReader:
    """Current fields by target: the entry's run albums first, the whole library if needed."""

    def __init__(self, backend: LibraryBackend) -> None:
        self.backend = backend
        self._runs: dict[str, SnapshotIndex] = {}
        self._all: SnapshotIndex | None = None

    def for_entry(self, entry: JournalEntry) -> Callable[[TargetRef], dict[str, FieldValue] | None]:
        refs = [t.target for t in entry.intended]
        index = None
        if entry.run_id is not None:
            index = self._runs.get(entry.run_id)
            if index is None:
                index = SnapshotIndex()
                index.add(self.backend.query(Selection(run_id=entry.run_id)))
                self._runs[entry.run_id] = index
        if index is None or any(index.fields(r) is None for r in refs):
            if self._all is None:
                self._all = SnapshotIndex()
                self._all.add(self.backend.query(Selection()))
            index = self._all
        return index.fields


def recover_pending(
    profile: str,
    *,
    journal: Journal,
    backend: LibraryBackend,
    store: ChangeSetStore,
    emitter_for: EmitterFor,
    now: Callable[[], datetime],
) -> list[JournalEntry]:
    """Settle every pending entry of ``profile``; returns the settled entries."""
    reader = _LibraryReader(backend)
    settled: list[JournalEntry] = []
    for entry in journal.pending(profile):
        done = reconcile(entry, reader.for_entry(entry), now())
        journal.recover(done)
        settled.append(done)
        stored = store.get(entry.changeset_id)
        emit = emitter_for(entry.run_id, stored.changeset.step_id if stored is not None else None)
        data: dict[str, JsonValue] = {
            "changeset_id": entry.changeset_id,
            "journal_id": entry.id,
            "recovered": True,
        }
        if done.state == "applied":
            state: ChangeSetState = "partially_applied" if entry.accepted else "applied"
            if stored is not None:
                store.set_state(entry.changeset_id, state)
            if entry.undo_of is not None:
                _finish_recovered_undo(entry, journal, store)
            emit("changeset.applied", {**data, "uncertain": done.uncertain})
        else:
            # Nothing changed: the ChangeSet waits again. An interrupted undo is dropped.
            again: ChangeSetState = "rejected" if entry.undo_of is not None else "proposed"
            if stored is not None:
                store.set_state(entry.changeset_id, again)
            emit(
                "changeset.rejected" if again == "rejected" else "changeset.proposed",
                {**data, "error": INTERRUPTED},
            )
    return settled


def _finish_recovered_undo(entry: JournalEntry, journal: Journal, store: ChangeSetStore) -> None:
    assert entry.undo_of is not None
    original = journal.get(entry.undo_of)
    if original is None or original.undone_by is not None:
        return
    journal.mark_undone(original.id, entry.id)
    if store.get(original.changeset_id) is not None:
        store.set_state(original.changeset_id, "undone")
