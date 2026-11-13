"""Proposing, storing and applying ChangeSets (C-CHANGESET).

Pure apart from injected interfaces: the backend, the journal, the ChangeSet
store, a clock and an id factory. Only the engine applies ChangeSets, always
through ``apply_changeset``, which journals the intent first (CHANGESET-13,
JOURNAL-1).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from rifferr.backends.base import ChangeSetRejected, LibraryBackend
from rifferr.core.changeset import (
    ChangeProblem,
    ChangeSet,
    ChangeSetProposal,
    ChangeSetState,
    ChangeSource,
    FieldChange,
    JournalEntry,
    ProposedTargetChange,
    TargetChange,
    TargetRef,
    same_value,
)
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, label
from rifferr.engine.journal import Journal

#: (event type, data) -> None. Events go on the run's stream (CHANGESET-5).
EmitChange = Callable[[str, dict[str, JsonValue]], None]


class ProposalError(ValueError):
    """A proposal the engine can't turn into ChangeSets (fails the step)."""


@dataclass(frozen=True)
class StoredChangeSet:
    changeset: ChangeSet
    state: ChangeSetState
    resolved_by: str | None = None
    decisions: dict[int, bool] = field(default_factory=dict[int, bool])
    """Field-change index -> accepted, once review decided (CHANGESET-9)."""


class ChangeSetStore(Protocol):
    """Persisted ChangeSets with their state (CHANGESET-5). The app DB in the worker."""

    def add(self, changeset: ChangeSet) -> None:
        """Store a new ChangeSet in state ``proposed``."""
        ...

    def get(self, changeset_id: str) -> StoredChangeSet | None: ...

    def set_state(
        self,
        changeset_id: str,
        state: ChangeSetState,
        *,
        resolved_by: str | None = None,
        decisions: dict[int, bool] | None = None,
    ) -> None: ...

    def for_run(self, run_id: str, state: ChangeSetState | None = None) -> list[StoredChangeSet]:
        """ChangeSets of ``run_id`` (optionally in ``state``), oldest first."""
        ...


class InMemoryChangeSetStore:
    def __init__(self) -> None:
        self.items: dict[str, StoredChangeSet] = {}

    def add(self, changeset: ChangeSet) -> None:
        if changeset.id in self.items:
            raise ValueError(f"ChangeSet {changeset.id} exists")
        self.items[changeset.id] = StoredChangeSet(changeset, "proposed")

    def get(self, changeset_id: str) -> StoredChangeSet | None:
        return self.items.get(changeset_id)

    def set_state(
        self,
        changeset_id: str,
        state: ChangeSetState,
        *,
        resolved_by: str | None = None,
        decisions: dict[int, bool] | None = None,
    ) -> None:
        old = self.items[changeset_id]
        self.items[changeset_id] = StoredChangeSet(
            old.changeset,
            state,
            resolved_by if resolved_by is not None else old.resolved_by,
            dict(decisions) if decisions is not None else old.decisions,
        )

    def for_run(self, run_id: str, state: ChangeSetState | None = None) -> list[StoredChangeSet]:
        return [
            s
            for s in self.items.values()
            if s.changeset.run_id == run_id and (state is None or s.state == state)
        ]


# -- proposing (CHANGESET-3, -4, -14) ---------------------------------------------


class SnapshotIndex:
    """Fields of every album and item a step was given, and item -> album membership.

    ``ctx.albums()`` feeds it; ``propose`` fills ``old`` and drops no-ops from it.
    """

    def __init__(self) -> None:
        self._fields: dict[TargetRef, dict[str, FieldValue]] = {}
        self._album_of: dict[str, str] = {}
        self._albums: dict[str, AlbumSnapshot] = {}

    def add(self, albums: Iterable[AlbumSnapshot]) -> None:
        for album in albums:
            self._albums[album.id] = album
            self._fields[TargetRef(scope="album", id=album.id)] = dict(album.fields)
            for item in album.items:
                self._fields[TargetRef(scope="item", id=item.id)] = dict(item.fields)
                self._album_of[item.id] = album.id

    def fields(self, ref: TargetRef) -> dict[str, FieldValue] | None:
        return self._fields.get(ref)

    def album(self, album_id: str) -> AlbumSnapshot | None:
        """The latest snapshot of an album the step was given."""
        return self._albums.get(album_id)

    def album_of(self, ref: TargetRef) -> str:
        return ref.id if ref.scope == "album" else self._album_of[ref.id]


def build_changesets(
    proposal: ChangeSetProposal,
    *,
    current: TargetRef | None,
    snapshots: SnapshotIndex,
    make: Callable[[list[TargetChange], str | None], ChangeSet],
) -> list[ChangeSet]:
    """Resolve the short form, fill ``old``, drop no-ops and split per album.

    ``make(targets, rationale)`` adds the ids and provenance. Returns ``[]`` when
    every field change was a no-op.
    """
    if proposal.changes is not None:
        if current is None:
            raise ProposalError("the short form 'changes' needs a current album or item")
        targets = [ProposedTargetChange(target=current, changes=proposal.changes)]
    else:
        assert proposal.targets is not None
        targets = proposal.targets
    groups: dict[str, list[TargetChange]] = {}
    for t in targets:
        fields = snapshots.fields(t.target)
        if fields is None:
            raise ProposalError(f"{t.target.label()} is not one of this step's targets")
        kept: list[FieldChange] = []
        for c in t.changes:
            now = fields.get(c.field)
            if c.op == "delete" and c.field not in fields:
                continue  # deleting an absent field (CHANGESET-4)
            if c.op == "set" and c.field in fields and same_value(now, c.new):
                continue  # already the current value (CHANGESET-4)
            old = c.old if c.has_old() else now
            kept.append(
                FieldChange(field=c.field, op=c.op, old=old, new=c.new, rationale=c.rationale)
            )
        if kept:
            album = snapshots.album_of(t.target)
            groups.setdefault(album, []).append(TargetChange(target=t.target, changes=kept))
    return [make(changes, proposal.rationale) for changes in groups.values()]


def new_changeset(
    targets: list[TargetChange],
    rationale: str | None,
    *,
    id: str,
    profile: str,
    run_id: str | None,
    step_id: str | None,
    source: ChangeSource,
    created_at: datetime,
) -> ChangeSet:
    return ChangeSet(
        id=id,
        profile=profile,
        run_id=run_id,
        step_id=step_id,
        source=source,
        rationale=rationale,
        created_at=created_at,
        targets=targets,
    )


def proposed_event(changeset: ChangeSet) -> dict[str, JsonValue]:
    return {
        "changeset_id": changeset.id,
        "targets": [t.target.label() for t in changeset.targets],
        "field_changes": len(changeset.field_changes()),
    }


# -- applying (CHANGESET-6, -8, -9, JOURNAL-1, -2) ---------------------------------


@dataclass(frozen=True)
class ApplyOutcome:
    state: ChangeSetState
    entry: JournalEntry | None
    problems: list[ChangeProblem] = field(default_factory=list[ChangeProblem])


def apply_changeset(
    changeset: ChangeSet,
    *,
    backend: LibraryBackend,
    journal: Journal,
    store: ChangeSetStore,
    new_id: Callable[[], str],
    now: Callable[[], datetime],
    emit: EmitChange,
    accepted: list[int] | None = None,
    resolved_by: str | None = None,
    undo_of: str | None = None,
) -> ApplyOutcome:
    """Apply ``changeset`` (or the ``accepted`` field changes of it) through the journal.

    The journal entry is committed before ``backend.apply`` runs. Ends in
    ``applied``, ``partially_applied`` or ``conflicted``; refused changes
    (``check`` problems) end ``rejected`` without touching the journal.
    """
    total = len(changeset.field_changes())
    partial = accepted is not None and sorted(set(accepted)) != list(range(total))
    target = changeset.subset(accepted) if accepted is not None else changeset
    decisions = {i: i in set(accepted) for i in range(total)} if accepted is not None else None
    problems = backend.check(target)
    if problems:
        store.set_state(changeset.id, "rejected", resolved_by=resolved_by, decisions=decisions)
        emit(
            "changeset.rejected",
            {
                "changeset_id": changeset.id,
                "problems": [f"{p.target.label()} {p.field}: {p.message}" for p in problems],
            },
        )
        return ApplyOutcome("rejected", None, problems)
    entry = JournalEntry(
        id=new_id(),
        profile=changeset.profile,
        changeset_id=changeset.id,
        run_id=changeset.run_id,
        accepted=sorted(set(accepted)) if partial and accepted is not None else None,
        intended=target.targets,
        undo_of=undo_of,
        created_at=now(),
    )
    journal.begin(entry)  # durable before anything changes (JOURNAL-1)
    try:
        result = backend.apply(target)
    except ChangeSetRejected as exc:  # check() raced with another writer
        journal.fail(entry.id, str(exc))
        store.set_state(changeset.id, "rejected", resolved_by=resolved_by, decisions=decisions)
        emit("changeset.rejected", {"changeset_id": changeset.id, "problems": [str(exc)]})
        return ApplyOutcome("rejected", journal.get(entry.id), exc.problems)
    except Exception as exc:
        journal.fail(entry.id, f"{type(exc).__name__}: {exc}")
        raise
    entry = journal.complete(entry.id, result)
    if not result.applied:
        store.set_state(changeset.id, "conflicted", resolved_by=resolved_by, decisions=decisions)
        emit(
            "changeset.conflicted",
            {
                "changeset_id": changeset.id,
                "journal_id": entry.id,
                "conflicts": cast(
                    list[JsonValue],
                    [
                        {
                            "target": c.target.label(),
                            "field": c.field,
                            "expected": c.expected,
                            "current": c.current,
                        }
                        for c in result.conflicts
                    ],
                ),
            },
        )
        return ApplyOutcome("conflicted", entry)
    state: ChangeSetState = "partially_applied" if partial else "applied"
    store.set_state(changeset.id, state, resolved_by=resolved_by, decisions=decisions)
    emit(
        "changeset.applied",
        {
            "changeset_id": changeset.id,
            "journal_id": entry.id,
            "partial": partial,
            "effects": len(result.effects),
            "write_failures": [
                {"target": f.target.label(), "path": f.path, "error": f.error}
                for f in result.write_failures
            ],
        },
    )
    return ApplyOutcome(state, entry)


def reject_changeset(
    changeset_id: str, *, store: ChangeSetStore, emit: EmitChange, resolved_by: str | None = None
) -> None:
    store.set_state(changeset_id, "rejected", resolved_by=resolved_by)
    emit("changeset.rejected", {"changeset_id": changeset_id})


# -- current values and re-proposing (REVIEW-QUEUE-3, -5, CHANGESET-8) ---------------


class CurrentTarget(BaseModel):
    """What one target of a ChangeSet holds now (REVIEW-QUEUE-3)."""

    model_config = ConfigDict(extra="forbid")

    target: TargetRef
    label: str
    missing: bool = False
    fields: dict[str, FieldValue] = Field(
        default_factory=dict[str, FieldValue],
        description="Only the fields the ChangeSet changes; an absent field is left out",
    )


def current_values(backend: LibraryBackend, changeset: ChangeSet) -> list[CurrentTarget]:
    """The current values of the fields ``changeset`` changes, per target, read from the
    backend's snapshots. Writes nothing."""
    out: list[CurrentTarget] = []
    found: dict[TargetRef, AlbumSnapshot | None] = {}
    for scope in ("album", "item"):
        ids = tuple(t.target.id for t in changeset.targets if t.target.scope == scope)
        if not ids:
            continue
        for album in backend.query(Selection(scope=scope, ids=ids)):
            if scope == "album":
                found[TargetRef(scope="album", id=album.id)] = album
            for item in album.items if scope == "item" else []:
                found[TargetRef(scope="item", id=item.id)] = album
    for t in changeset.targets:
        album = found.get(t.target)
        if album is None:
            out.append(CurrentTarget(target=t.target, label=t.target.label(), missing=True))
            continue
        snap = (
            album
            if t.target.scope == "album"
            else next(i for i in album.items if i.id == t.target.id)
        )
        wanted = {c.field for c in t.changes}
        out.append(
            CurrentTarget(
                target=t.target,
                label=label(snap),
                fields={k: v for k, v in snap.fields.items() if k in wanted},
            )
        )
    return out


def repropose_targets(
    changeset: ChangeSet, current: list[CurrentTarget], accepted: list[int] | None = None
) -> list[TargetChange]:
    """The field changes of ``changeset`` again, each ``old`` taken from ``current``
    (CHANGESET-8). Only the ``accepted`` ones if review accepted a subset; no-ops on
    the current values (CHANGESET-4) and targets that no longer exist are dropped."""
    now = {c.target: c for c in current}
    wanted = None if accepted is None else set(accepted)
    out: list[TargetChange] = []
    index = 0
    for t in changeset.targets:
        kept: list[FieldChange] = []
        cur = now.get(t.target)
        for c in t.changes:
            i, index = index, index + 1
            if wanted is not None and i not in wanted:
                continue
            if cur is None or cur.missing:
                continue
            present = c.field in cur.fields
            value = cur.fields.get(c.field)
            if c.op == "delete" and not present:
                continue
            if c.op == "set" and present and same_value(value, c.new):
                continue
            kept.append(c.model_copy(update={"old": value}))
        if kept:
            out.append(TargetChange(target=t.target, changes=kept))
    return out
