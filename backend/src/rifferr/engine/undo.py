"""Undo of journal entries and runs (C-JOURNAL JOURNAL-4, -5, -6).

Undo builds the inverse ChangeSet of an entry's effects and applies it through
the normal path: journaled first, all or nothing, conflict-checked. If a field
changed again since the apply, nothing is undone and the conflict is reported.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from pydantic import JsonValue

from rifferr.backends.base import LibraryBackend
from rifferr.core.changeset import (
    ChangeProblem,
    ChangeSet,
    ChangeSource,
    FieldChange,
    FieldEffect,
    JournalEntry,
    TargetChange,
    TargetRef,
)
from rifferr.core.snapshot import FieldValue
from rifferr.engine.changesets import ChangeSetStore, apply_changeset, proposed_event
from rifferr.engine.journal import Journal
from rifferr.engine.recovery import EmitterFor

UNDO_SOURCE = ChangeSource(kind="manual", name="undo")


class UndoError(ValueError):
    """The entry or run can't be undone (unknown, not applied, already undone)."""


@dataclass(frozen=True)
class UndoConflict:
    target: TargetRef
    field: str
    expected: FieldValue
    current: FieldValue
    would_restore: FieldValue

    def as_json(self) -> dict[str, JsonValue]:
        data: dict[str, object] = {
            "target": self.target.label(),
            "field": self.field,
            "expected": self.expected,
            "current": self.current,
            "would_restore": self.would_restore,
        }
        return cast(dict[str, JsonValue], data)


@dataclass(frozen=True)
class UndoOutcome:
    entry_id: str
    undone: bool
    undo_entry_id: str | None = None
    """The journal entry of the undo itself (``undo_of`` = ``entry_id``)."""
    conflicts: list[UndoConflict] = field(default_factory=list[UndoConflict])
    problems: list[ChangeProblem] = field(default_factory=list[ChangeProblem])


@dataclass(frozen=True)
class RunUndoOutcome:
    run_id: str
    undone: list[UndoOutcome] = field(default_factory=list[UndoOutcome])
    stopped: UndoOutcome | None = None
    """The undo that conflicted or was refused; later (older) entries were left alone."""

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "undone": [o.entry_id for o in self.undone],
            "undo_entries": [o.undo_entry_id for o in self.undone],
            "stopped_at": self.stopped.entry_id if self.stopped else None,
            "conflicts": [c.as_json() for c in self.stopped.conflicts] if self.stopped else [],
            "problems": [
                f"{p.target.label()} {p.field}: {p.message}" for p in self.stopped.problems
            ]
            if self.stopped
            else [],
        }


def inverse(effects: list[FieldEffect]) -> list[TargetChange]:
    """Each effect's ``after`` back to ``before``, expecting ``after`` (JOURNAL-4).

    Fields that didn't exist before are deleted. Targets keep the order of the
    effects (an album before its items), so item values win over inherited ones.
    """
    grouped: dict[TargetRef, list[FieldChange]] = {}
    for e in effects:
        if e.existed_before and e.before is not None:
            change = FieldChange(field=e.field, op="set", old=e.after, new=e.before)
        else:
            change = FieldChange(field=e.field, op="delete", old=e.after)
        grouped.setdefault(e.target, []).append(change)
    return [TargetChange(target=t, changes=c) for t, c in grouped.items()]


def undoable(entry: JournalEntry | None, entry_id: str) -> JournalEntry:
    if entry is None:
        raise UndoError(f"no journal entry {entry_id}")
    if entry.state != "applied":
        raise UndoError(f"journal entry {entry_id} is {entry.state}; only applied entries undo")
    if entry.undone_by is not None:
        raise UndoError(f"journal entry {entry_id} is already undone by {entry.undone_by}")
    if not entry.effects:
        raise UndoError(f"journal entry {entry_id} changed nothing")
    return entry


@dataclass(frozen=True)
class UndoContext:
    backend: LibraryBackend
    journal: Journal
    store: ChangeSetStore
    new_id: Callable[[], str]
    now: Callable[[], datetime]
    emitter_for: EmitterFor
    by: str | None = None


def undo_entry(entry_id: str, ctx: UndoContext) -> UndoOutcome:
    """Undo one applied entry (JOURNAL-4, -5). Raises ``UndoError`` if it can't be."""
    entry = undoable(ctx.journal.get(entry_id), entry_id)
    original = ctx.store.get(entry.changeset_id)
    step_id = original.changeset.step_id if original is not None else None
    emit = ctx.emitter_for(entry.run_id, step_id)
    targets = inverse(entry.effects)
    cs = ChangeSet(
        id=ctx.new_id(),
        profile=entry.profile,
        run_id=entry.run_id,
        step_id=step_id,
        source=UNDO_SOURCE,
        rationale=f"undo of journal entry {entry.id}",
        created_at=ctx.now(),
        targets=targets,
    )
    ctx.store.add(cs)
    emit("changeset.proposed", {**proposed_event(cs), "undo_of": entry.id})
    outcome = apply_changeset(
        cs,
        backend=ctx.backend,
        journal=ctx.journal,
        store=ctx.store,
        new_id=ctx.new_id,
        now=ctx.now,
        emit=emit,
        resolved_by=ctx.by,
        undo_of=entry.id,
    )
    if outcome.state == "applied":
        assert outcome.entry is not None
        ctx.journal.mark_undone(entry.id, outcome.entry.id)
        if original is not None:
            ctx.store.set_state(entry.changeset_id, "undone", resolved_by=ctx.by)
        emit(
            "changeset.undone",
            {
                "changeset_id": entry.changeset_id,
                "journal_id": entry.id,
                "undo_changeset_id": cs.id,
                "undo_journal_id": outcome.entry.id,
            },
        )
        return UndoOutcome(entry.id, True, outcome.entry.id)
    restore = {(t.target, c.field): c.new for t in targets for c in t.changes}
    conflicts = [
        UndoConflict(c.target, c.field, c.expected, c.current, restore.get((c.target, c.field)))
        for c in (outcome.entry.conflicts if outcome.entry is not None else [])
    ]
    return UndoOutcome(
        entry.id,
        False,
        outcome.entry.id if outcome.entry is not None else None,
        conflicts,
        outcome.problems,
    )


def run_entries(journal: Journal, run_id: str) -> list[JournalEntry]:
    """A run's applied, not yet undone entries, newest first (JOURNAL-6).

    Undo entries themselves are left out: undoing a run reverts what the run did.
    """
    entries = [
        e
        for e in journal.for_run(run_id)
        if e.state == "applied" and e.undone_by is None and e.undo_of is None and e.effects
    ]
    return list(reversed(entries))


def undo_run(run_id: str, ctx: UndoContext) -> RunUndoOutcome:
    """Undo a run's entries newest first, stopping at the first conflict (JOURNAL-6)."""
    done: list[UndoOutcome] = []
    for entry in run_entries(ctx.journal, run_id):
        outcome = undo_entry(entry.id, ctx)
        if not outcome.undone:
            return RunUndoOutcome(run_id, done, outcome)
        done.append(outcome)
    return RunUndoOutcome(run_id, done)
