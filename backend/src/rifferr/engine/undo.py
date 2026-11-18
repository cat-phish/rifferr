"""Undo of journal entries and runs (C-JOURNAL JOURNAL-4, -5, -6).

Undo builds the inverse ChangeSet of an entry's effects and applies it through
the normal path: journaled first, all or nothing, conflict-checked. If a field
changed again since the apply, nothing is undone and the conflict is reported.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
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
    FileEffect,
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
    changed_files: list[str] = field(default_factory=list[str])
    """Files changed since the move: undo refused (LIBRARY-MOVE-15, HTTP 409)."""
    skipped_files: list[str] = field(default_factory=list[str])
    """Files undo left alone because the other copy is missing or differs (-13, -14):
    the undo is incomplete and the original entry stays not undone."""
    error: str | None = None


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
            "changed_files": list(self.stopped.changed_files) if self.stopped else [],
            "skipped_files": list(self.stopped.skipped_files) if self.stopped else [],
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


def inverse_files(
    files: list[FileEffect],
    stat: Callable[[str], tuple[int, str] | None],
    force: Collection[str] = (),
) -> tuple[list[FileEffect], list[str]]:
    """The inverse plan of a file apply, newest first (LIBRARY-MOVE-13, -14), and the files
    that changed since (LIBRARY-MOVE-15). ``stat`` reads size and SHA-256 from the
    filesystem. A changed file whose destination is in ``force`` goes back anyway, as it
    is now (a move), or is dropped for its unchanged original (a copy).

    - a finished move goes back by the same copy-verify-remove path;
    - where both copies exist (a copy, or a move stopped after copying), the library copy
      is removed only if the original is still there with the same hash. Otherwise the file
      is ``skipped``: undo never deletes the only copy.
    """
    out: list[FileEffect] = []
    changed: list[str] = []
    for f in reversed(files):
        if f.state not in ("copied", "done"):
            continue
        back = f.model_copy(update={"src": f.dst, "dst": f.src, "mode": "move", "note": None})
        now = stat(f.dst)
        if now is None:
            out.append(back.model_copy(update={"state": "skipped", "note": f"{f.dst} is missing"}))
            continue
        if now[1] != f.sha256 and f.dst not in force:
            changed.append(f.dst)
            continue
        both = f.mode == "copy" or f.state == "copied"
        if not both:  # moved back as it is now: the verified copy checks today's content
            size, sha = now
            out.append(back.model_copy(update={"state": "planned", "size": size, "sha256": sha}))
        elif (info := stat(f.src)) is not None and info[1] == f.sha256:
            out.append(back.model_copy(update={"state": "copied"}))
        else:
            note = f"{f.src} is missing or changed; {f.dst} is the only copy"
            out.append(back.model_copy(update={"state": "skipped", "note": note}))
    return out, changed


def undoable(entry: JournalEntry | None, entry_id: str) -> JournalEntry:
    if entry is None:
        raise UndoError(f"no journal entry {entry_id}")
    if entry.state not in ("applied", "partial") or (entry.state == "partial" and not entry.files):
        raise UndoError(f"journal entry {entry_id} is {entry.state}; only applied entries undo")
    if entry.undone_by is not None:
        raise UndoError(f"journal entry {entry_id} is already undone by {entry.undone_by}")
    if not entry.effects and not any(f.state in ("copied", "done") for f in entry.files):
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
    force: frozenset[str] = frozenset()
    """Destination files to undo although they changed since the move (LIBRARY-MOVE-15)."""


def undo_entry(entry_id: str, ctx: UndoContext) -> UndoOutcome:
    """Undo one applied entry (JOURNAL-4, -5). Raises ``UndoError`` if it can't be."""
    entry = undoable(ctx.journal.get(entry_id), entry_id)
    original = ctx.store.get(entry.changeset_id)
    step_id = original.changeset.step_id if original is not None else None
    emit = ctx.emitter_for(entry.run_id, step_id)
    targets = inverse(entry.effects)
    files: list[FileEffect] = []
    if entry.files:
        files, changed = inverse_files(entry.files, ctx.backend.file_stat, ctx.force)
        if changed:
            return UndoOutcome(entry.id, False, changed_files=changed)
    cs = ChangeSet(
        id=ctx.new_id(),
        profile=entry.profile,
        run_id=entry.run_id,
        step_id=step_id,
        source=UNDO_SOURCE,
        rationale=f"undo of journal entry {entry.id}",
        created_at=ctx.now(),
        targets=targets,
        files=files,
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
    skipped = [f"{f.src}: {f.note}" for f in files if f.state == "skipped"]
    if outcome.state == "applied" and skipped:  # undo incomplete: stays in review (-13)
        assert outcome.entry is not None
        emit(
            "changeset.undo_incomplete",
            {
                "changeset_id": entry.changeset_id,
                "journal_id": entry.id,
                "skipped": cast(list[JsonValue], skipped),
            },
        )
        return UndoOutcome(entry.id, False, outcome.entry.id, skipped_files=skipped)
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
        error=outcome.error,
    )


def run_entries(journal: Journal, run_id: str) -> list[JournalEntry]:
    """A run's applied, not yet undone entries, newest first (JOURNAL-6).

    Undo entries themselves are left out: undoing a run reverts what the run did.
    """
    entries = [
        e
        for e in journal.for_run(run_id)
        if e.state in ("applied", "partial")
        and e.undone_by is None
        and e.undo_of is None
        and (e.effects or any(f.state in ("copied", "done") for f in e.files))
    ]
    return list(reversed(entries))


def undo_run(
    run_id: str, ctx: UndoContext, *, targets: Collection[str] | None = None
) -> RunUndoOutcome:
    """Undo a run's entries newest first, stopping at the first conflict (JOURNAL-6).

    With ``targets`` (album and item ids), only entries touching one of them are undone:
    a gate rejection undoes just its albums (C-LANES LANES-11).
    """
    done: list[UndoOutcome] = []
    for entry in run_entries(ctx.journal, run_id):
        if targets is not None and not any(t.target.id in targets for t in entry.intended):
            continue
        outcome = undo_entry(entry.id, ctx)
        if not outcome.undone:
            return RunUndoOutcome(run_id, done, outcome)
        done.append(outcome)
    return RunUndoOutcome(run_id, done)
