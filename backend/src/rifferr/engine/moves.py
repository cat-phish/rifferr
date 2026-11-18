"""Move conflicts on the engine side (S-LIBRARY-MOVE LIBRARY-MOVE-5a)."""

from __future__ import annotations

from typing import Protocol

from rifferr.core.moves import MoveConflict


class MoveConflictStore(Protocol):
    """Persisted move conflicts. The app DB in the worker."""

    def add(self, conflict: MoveConflict) -> None: ...

    def get(self, conflict_id: str) -> MoveConflict | None: ...


class InMemoryMoveConflictStore:
    def __init__(self) -> None:
        self.items: dict[str, MoveConflict] = {}

    def add(self, conflict: MoveConflict) -> None:
        if conflict.id in self.items:
            raise ValueError(f"move conflict {conflict.id} exists")
        self.items[conflict.id] = conflict

    def get(self, conflict_id: str) -> MoveConflict | None:
        return self.items.get(conflict_id)
