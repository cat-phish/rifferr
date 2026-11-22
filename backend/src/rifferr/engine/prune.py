"""Library-prune decisions and removal records on the engine side (S-LIBRARY-PRUNE)."""

from __future__ import annotations

from typing import Literal, Protocol

from rifferr.core.prune import PruneDecision
from rifferr.core.snapshot import AlbumSnapshot


class PruneDecisionStore(Protocol):
    """Persisted prune decisions. The app DB in the worker."""

    def add(self, decision: PruneDecision) -> None: ...

    def get(self, decision_id: str) -> PruneDecision | None: ...


class InMemoryPruneStore:
    def __init__(self) -> None:
        self.items: dict[str, PruneDecision] = {}

    def add(self, decision: PruneDecision) -> None:
        if decision.id in self.items:
            raise ValueError(f"prune decision {decision.id} exists")
        self.items[decision.id] = decision

    def get(self, decision_id: str) -> PruneDecision | None:
        return self.items.get(decision_id)


class RemovalJournal(Protocol):
    """Write-ahead records of album removals (LIBRARY-PRUNE-4): ``begin`` before the
    removal, ``finish`` after. The app DB's ``album_removals`` in the worker."""

    def begin(
        self, run_id: str, album: AlbumSnapshot, label: str, *, delete_files: bool
    ) -> str: ...

    def finish(
        self,
        record_id: str,
        state: Literal["removed", "failed"],
        *,
        deleted: list[str] | None = None,
        error: str | None = None,
    ) -> None: ...


class InMemoryRemovals:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}

    def begin(self, run_id: str, album: AlbumSnapshot, label: str, *, delete_files: bool) -> str:
        record_id = f"r{len(self.records) + 1}"
        self.records[record_id] = {
            "run_id": run_id,
            "album_id": album.id,
            "label": label,
            "delete_files": delete_files,
            "state": "pending",
        }
        return record_id

    def finish(
        self,
        record_id: str,
        state: Literal["removed", "failed"],
        *,
        deleted: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        self.records[record_id].update(state=state, deleted=deleted or [], error=error)
