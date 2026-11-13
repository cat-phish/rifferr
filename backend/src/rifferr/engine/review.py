"""Review gates on the engine side (S-REVIEW): the gate store and "changes so far".

Pure apart from injected interfaces.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from rifferr.core.changeset import FieldEffect, JournalEntry, TargetRef, same_value
from rifferr.core.review import ReviewGate
from rifferr.core.snapshot import AlbumSnapshot
from rifferr.engine.changesets import StoredChangeSet


class GateStore(Protocol):
    """Persisted review gates (REVIEW-3). The app DB in the worker."""

    def add(self, gate: ReviewGate) -> None: ...

    def get(self, gate_id: str) -> ReviewGate | None: ...


class InMemoryGateStore:
    def __init__(self) -> None:
        self.items: dict[str, ReviewGate] = {}

    def add(self, gate: ReviewGate) -> None:
        if gate.id in self.items:
            raise ValueError(f"review gate {gate.id} exists")
        self.items[gate.id] = gate

    def get(self, gate_id: str) -> ReviewGate | None:
        return self.items.get(gate_id)


def album_targets(album: AlbumSnapshot) -> set[tuple[str, str]]:
    """``(scope, id)`` of the album and its items."""
    return {("album", album.id)} | {("item", i.id) for i in album.items}


def _key(ref: TargetRef) -> tuple[str, str]:
    return (ref.scope, ref.id)


def fold_changes(entries: Iterable[JournalEntry], album: AlbumSnapshot) -> list[FieldEffect]:
    """The run's net changes to ``album`` and its items (REVIEW-4).

    Applied entries that aren't undone (and aren't undos themselves), oldest first:
    per target and field the first ``before`` and the last ``after``. Fields that
    ended where they started are left out.
    """
    targets = album_targets(album)
    first: dict[tuple[TargetRef, str], FieldEffect] = {}
    last: dict[tuple[TargetRef, str], FieldEffect] = {}
    for entry in entries:
        if entry.state != "applied" or entry.undone_by is not None or entry.undo_of is not None:
            continue
        for effect in entry.effects:
            if _key(effect.target) not in targets:
                continue
            key = (effect.target, effect.field)
            first.setdefault(key, effect)
            last[key] = effect
    out: list[FieldEffect] = []
    for key, start in first.items():
        end = last[key]
        if start.existed_before and same_value(start.before, end.after) and end.after is not None:
            continue  # changed and changed back
        if not start.existed_before and end.after is None:
            continue  # added, then deleted again
        out.append(
            FieldEffect(
                target=start.target,
                field=start.field,
                before=start.before,
                after=end.after,
                existed_before=start.existed_before,
            )
        )
    return out


def conflicted_for(changesets: Iterable[StoredChangeSet], album: AlbumSnapshot) -> list[str]:
    """Ids of conflicted ChangeSets that touch ``album`` or its items (REVIEW-4)."""
    targets = album_targets(album)
    return [
        s.changeset.id
        for s in changesets
        if s.state == "conflicted" and any(_key(t.target) in targets for t in s.changeset.targets)
    ]
